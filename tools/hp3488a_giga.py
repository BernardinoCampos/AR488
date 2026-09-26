#!/usr/bin/env python3
"""
Giga de testes: interface AR488 (SN75160/SN75161) + HP 3488A Switch/Control Unit.

Etapas:
  1. Interface AR488: versao, modo controller, suporte SN7516x (++ren -> Unavailable)
  2. HP 3488A: identificacao (ID?), autoteste (TEST), registrador de erro (ERROR)
  3. Placas instaladas nos slots 1-5 (CTYPE)
  4. Reles: para cada canal, abre/fecha e confere o estado (VIEW) e o registrador
     de erro. Um canal por vez; o estado original de cada canal e restaurado.

Obs.: VIEW informa o estado comandado pelo 3488A, nao a continuidade real do
contato. Para verificar contatos use --pause e um ohmimetro nos bornes da placa.

Uso:
  python3 tools/hp3488a_giga.py [--port /dev/cu.usbserial-XXXX] [--addr 9]
                                [--cycles N] [--no-relays] [--pause]
Requer: pyserial
"""
import argparse
import glob
import sys
import time

import serial

# Canais por modelo (3488A: slot*100 + canal)
RANGES = {
    '44470': list(range(0, 10)),                       # 10-ch relay mux
    '44471': list(range(0, 10)),                       # 10-ch GP (44477A: 0-6)
    '44472': [0, 1, 2, 3, 10, 11, 12, 13],             # dual 4-ch VHF
    '44473': [r * 10 + c for r in range(4) for c in range(4)],  # 4x4 matrix
}
NO_RELAYS = {'44474': 'DIGITAL IO (sem reles)', '44475': 'BREADBOARD (sem reles)'}


class AR488:
    def __init__(self, port, baud=115200):
        self.p = serial.Serial(port, baud, timeout=0.2)
        time.sleep(2.5)                      # auto-reset do Arduino ao abrir a porta
        self.p.reset_input_buffer()

    def _rx(self, timeout=3.0):
        end = time.time() + timeout
        buf = b''
        while time.time() < end:
            buf += self.p.read(256)
            if buf.endswith(b'\n'):
                break
        return buf.decode(errors='replace').strip()

    def cmd(self, c, reply=False, timeout=3.0):
        """Comando para o AR488 (++xxx) ou escrita para o instrumento."""
        self.p.reset_input_buffer()
        self.p.write((c + '\n').encode())
        if reply:
            return self._rx(timeout)
        time.sleep(0.05)
        return None

    def query(self, c, delay=0.1, timeout=3.0):
        """Envia ao instrumento e le a resposta ate EOI."""
        self.cmd(c)
        time.sleep(delay)
        return self.cmd('++read eoi', reply=True, timeout=timeout)


class Report:
    def __init__(self):
        self.fails = 0
        self.passes = 0

    def check(self, name, ok, detail=''):
        tag = 'PASS' if ok else 'FAIL'
        if ok:
            self.passes += 1
        else:
            self.fails += 1
        print(f'  [{tag}] {name}' + (f'  ({detail})' if detail else ''))
        return ok

    @staticmethod
    def info(msg):
        print(f'  [INFO] {msg}')


def find_port():
    ports = sorted(glob.glob('/dev/cu.usbserial*') + glob.glob('/dev/cu.usbmodem*')
                   + glob.glob('/dev/ttyUSB*') + glob.glob('/dev/ttyACM*'))
    if not ports:
        sys.exit('Nenhuma porta serial encontrada; use --port')
    return ports[0]


def state(resp):
    r = resp.upper()
    if 'CLOSED' in r:
        return 'CLOSED'
    if 'OPEN' in r:
        return 'OPEN'
    return None


def test_interface(gi, rep):
    print('\n== 1. Interface AR488 ==')
    ver = gi.cmd('++ver', reply=True)
    rep.check('Firmware responde', ver.startswith('AR488'), ver)
    mode = gi.cmd('++mode', reply=True)
    rep.check('Modo controller', mode == '1', f'++mode = {mode}')
    ren = gi.cmd('++ren 1', reply=True)
    rep.check('Suporte SN7516x compilado (DC via REN)', 'Unavailable' in ren, f'++ren 1 -> {ren}')


def test_instrument(gi, rep, addr):
    print(f'\n== 2. HP 3488A (endereco {addr}) ==')
    gi.cmd(f'++addr {addr}')
    gi.cmd('++auto 0')
    gi.cmd('++read_tmo_ms 2000')
    idn = gi.query('ID?')
    if not rep.check('Identificacao (ID?)', idn == 'HP3488A', idn or 'sem resposta'):
        return False
    old = gi.query('ERROR')
    if old not in ('+00000', ''):
        rep.info(f'Erro pendente anterior limpo: {old}')
    test = gi.query('TEST', delay=4.0, timeout=6.0)
    rep.check('Autoteste interno (TEST)', test == '+00000', test)
    err = gi.query('ERROR')
    rep.check('Registrador de erro limpo', err == '+00000', err)
    return True


def list_cards(gi, rep):
    print('\n== 3. Placas instaladas ==')
    cards = {}
    for slot in range(1, 6):
        s = gi.query(f'CTYPE {slot}')
        model = s.split()[-1] if s else ''
        name = ' '.join(s.split()[:-1]) if s else 'sem resposta'
        if model and model != '00000':
            cards[slot] = model
            print(f'  Slot {slot}: {name} (HP {model})')
        else:
            print(f'  Slot {slot}: vazio')
    rep.check('Ao menos uma placa detectada', bool(cards))
    return cards


def probe_channels(gi, slot, chans):
    """Mantem so os canais que o 3488A aceita (ex.: 44477A tem 0-6, nao 0-9)."""
    valid = []
    for ch in chans:
        v = gi.query(f'VIEW {slot * 100 + ch}')
        err = gi.query('ERROR')
        if state(v) and err == '+00000':
            valid.append(ch)
    return valid


def test_relays(gi, rep, slot, model, cycles, pause):
    chans = probe_channels(gi, slot, RANGES[model])
    variant = ''
    if model == '44471':
        variant = ' -> 44477A (7 reles form C)' if len(chans) == 7 else ' -> 44471A (10 reles SPST)'
    print(f'\n== 4. Reles slot {slot} (HP {model}): {len(chans)} canais{variant} ==')
    for ch in chans:
        addr = slot * 100 + ch
        orig = state(gi.query(f'VIEW {addr}'))
        ok = True
        detail = []
        for _ in range(cycles):
            for action, expect in (('CLOSE', 'CLOSED'), ('OPEN', 'OPEN')):
                gi.cmd(f'{action} {addr}')
                time.sleep(0.03)
                st = state(gi.query(f'VIEW {addr}'))
                err = gi.query('ERROR')
                if st != expect or err != '+00000':
                    ok = False
                    detail.append(f'{action}: VIEW={st} ERROR={err}')
                if pause and action == 'CLOSE' and cycles == 1:
                    input(f'    Canal {addr} FECHADO - meça continuidade e tecle Enter...')
        if orig == 'CLOSED':
            gi.cmd(f'CLOSE {addr}')     # restaura estado original
        rep.check(f'Canal {addr:3d}  fecha/abre x{cycles}', ok, '; '.join(detail))


def main():
    ap = argparse.ArgumentParser(description='Giga de testes AR488 + HP 3488A')
    ap.add_argument('--port', default=None)
    ap.add_argument('--baud', type=int, default=115200)
    ap.add_argument('--addr', type=int, default=9)
    ap.add_argument('--cycles', type=int, default=1, help='ciclos fecha/abre por rele')
    ap.add_argument('--no-relays', action='store_true', help='nao aciona os reles')
    ap.add_argument('--pause', action='store_true', help='pausa com cada rele fechado')
    a = ap.parse_args()

    port = a.port or find_port()
    print(f'Porta: {port}')
    gi = AR488(port, a.baud)
    rep = Report()

    test_interface(gi, rep)
    if test_instrument(gi, rep, a.addr):
        cards = list_cards(gi, rep)
        for slot, model in cards.items():
            if model in NO_RELAYS:
                rep.info(f'Slot {slot}: {NO_RELAYS[model]} - teste de reles ignorado')
            elif model not in RANGES:
                rep.info(f'Slot {slot}: modelo {model} sem teste definido')
            elif not a.no_relays:
                test_relays(gi, rep, slot, model, a.cycles, a.pause)
        err = gi.query('ERROR')
        rep.check('Registrador de erro limpo ao final', err == '+00000', err)
        gi.cmd('++loc')                 # devolve o painel frontal ao operador

    print(f'\nResultado: {rep.passes} PASS, {rep.fails} FAIL -> '
          + ('TUDO OK' if rep.fails == 0 else 'FALHOU'))
    sys.exit(1 if rep.fails else 0)


if __name__ == '__main__':
    main()
