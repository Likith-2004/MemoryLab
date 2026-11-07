; Memory Lab TEST FIXTURE (not a game): cartridge-RAM probe. Licence: CC0-1.0.
; MBC5 + RAM + battery (type $1B), 32 KiB ROM, 4 x 8 KiB cartridge RAM banks.
; It only idles; tests write cartridge RAM directly to prove the trace digest covers it.

SECTION "header", ROM0[$0100]
	nop
	jp Start
	ds $150 - @, 0

SECTION "code", ROM0[$0150]
Start:
	di
.idle:
	jr .idle
