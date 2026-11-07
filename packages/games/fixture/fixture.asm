; Memory Lab TEST FIXTURE ROM -- not a game, never shipped to the public lab.
; Authored for Memory Lab (Milestone 1). Licence: CC0-1.0.
;
; Purpose: a tiny deterministic cartridge whose WRAM layout is known from the
; build's own symbol file, so the harness (step loop, state reader, save states,
; determinism checks) can be tested without any third-party ROM.
;
; Cartridge: ROM only, 32 KiB, no RAM, no battery, no RTC (type $00).

; hardware registers (defined locally: hardware.inc is not part of the rgbds release)
DEF rP1   EQU $FF00
DEF rDIV  EQU $FF04
DEF rLCDC EQU $FF40
DEF rSCY  EQU $FF42
DEF rSCX  EQU $FF43
DEF rLY   EQU $FF44
DEF rBGP  EQU $FF47
DEF LCDC_BG_ON_TILES8000 EQU $91   ; LCD on, BG on, BG/window tile data at $8000

; ---- WRAM layout (the state map in fixture.statemap.json must match fixture.sym) ----
SECTION "wram", WRAM0[$C000]
wMapId:        db          ; $C000
wX:            db          ; $C001
wY:            db          ; $C002
wDialog:       db          ; $C003
wMilestones:   dw          ; $C004 (little endian)
wFrames:       dw          ; $C006 frames since init
wRng:          db          ; $C008
wLock:         db          ; $C009 walk lock, frames remaining
wPrevJoy:      db          ; $C00A
wCurJoy:       db          ; $C00B
wInputFrames:  dw          ; $C00C frames with any button held
wPartyCount:   db          ; $C00E
wParty:        ds 6 * 4    ; $C00F species, level, hp, max_hp per slot
wFlags:        db          ; $C027
wDialogText:   ds 16       ; $C028 charmap text, $50 terminated
wInitDone:     db          ; $C038 set to $A5 when init completed

DEF WALK_LOCK EQU 8
DEF MAX_COORD EQU 15
DEF TEXT_END  EQU $50

NEWCHARMAP fixture
CHARMAP " ", $7F
CHARMAP "@", TEXT_END
CHARMAP "A", $80
CHARMAP "B", $81
CHARMAP "C", $82
CHARMAP "D", $83
CHARMAP "E", $84
CHARMAP "F", $85
CHARMAP "G", $86
CHARMAP "H", $87
CHARMAP "I", $88
CHARMAP "J", $89
CHARMAP "K", $8A
CHARMAP "L", $8B
CHARMAP "M", $8C
CHARMAP "N", $8D
CHARMAP "O", $8E
CHARMAP "P", $8F
CHARMAP "Q", $90
CHARMAP "R", $91
CHARMAP "S", $92
CHARMAP "T", $93
CHARMAP "U", $94
CHARMAP "V", $95
CHARMAP "W", $96
CHARMAP "X", $97
CHARMAP "Y", $98
CHARMAP "Z", $99

SECTION "header", ROM0[$0100]
	nop
	jp Start
	ds $150 - @, 0              ; rgbfix fills in the header

SECTION "code", ROM0[$0150]

Start:
	di
	ld sp, $DFFF
.waitVBlank:
	ldh a, [rLY]
	cp 144
	jr c, .waitVBlank
	xor a
	ldh [rLCDC], a              ; LCD off: VRAM is writable

	; clear WRAM state block
	ld hl, $C000
	ld bc, wInitDone - $C000 + 1
.clear:
	xor a
	ld [hl+], a
	dec bc
	ld a, b
	or c
	jr nz, .clear

	; initial state: map 1 at (0,0)
	ld a, 1
	ld [wMapId], a
	ld a, 2
	ld [wPartyCount], a
	ld hl, wParty
	ld a, 1
	ld [hl+], a                 ; species
	ld a, 5
	ld [hl+], a                 ; level
	ld a, 20
	ld [hl+], a                 ; hp
	ld [hl+], a                 ; max hp
	ld a, 4
	ld [hl+], a
	ld a, 3
	ld [hl+], a
	ld a, 12
	ld [hl+], a
	ld [hl+], a
	ld a, $5A
	ld [wRng], a
	ld a, TEXT_END              ; empty, terminated dialogue buffer
	ld [wDialogText], a

	; tile 1 = checkerboard, tile 0 stays blank
	ld hl, $8010
	ld b, 8
.tile:
	ld a, $AA
	ld [hl+], a
	ld a, $00
	ld [hl+], a
	ld a, $55
	ld [hl+], a
	ld a, $00
	ld [hl+], a
	dec b
	dec b
	jr nz, .tile

	; 32x32 background map: tile = (col xor row) & 1
	ld hl, $9800
.map:
	ld a, l
	ld b, a
	rrca
	rrca
	rrca
	rrca
	rrca
	xor b
	and 1
	ld [hl+], a
	ld a, h
	cp $9C
	jr nz, .map

	ld a, $E4
	ldh [rBGP], a
	ld a, LCDC_BG_ON_TILES8000
	ldh [rLCDC], a

	ld a, $A5
	ld [wInitDone], a

MainLoop:
.waitStart:
	ldh a, [rLY]
	cp 144
	jr nz, .waitStart

	call ReadJoy
	call UpdateRng
	call Counters
	call HandleInput
	call CheckMilestones
	call Present

.waitEnd:
	ldh a, [rLY]
	cp 144
	jr z, .waitEnd
	jr MainLoop

; a <- active-high buttons: bit0 Right 1 Left 2 Up 3 Down 4 A 5 B 6 Select 7 Start
ReadJoy:
	ld a, $20
	ldh [rP1], a
	ldh a, [rP1]
	ldh a, [rP1]
	ldh a, [rP1]
	ldh a, [rP1]
	and $0F
	ld b, a
	ld a, $10
	ldh [rP1], a
	ldh a, [rP1]
	ldh a, [rP1]
	ldh a, [rP1]
	ldh a, [rP1]
	and $0F
	swap a
	or b
	cpl
	ld b, a
	ld a, $30
	ldh [rP1], a
	ld a, [wCurJoy]
	ld [wPrevJoy], a
	ld a, b
	ld [wCurJoy], a
	ret

; rng = rng * 5 + 1; any newly pressed button also mixes in DIV
UpdateRng:
	ld a, [wRng]
	ld b, a
	add a, a
	add a, a
	add a, b
	inc a
	ld b, a
	ld a, [wPrevJoy]
	cpl
	ld c, a
	ld a, [wCurJoy]
	and c
	jr z, .store
	ldh a, [rDIV]
	xor b
	ld b, a
.store:
	ld a, b
	ld [wRng], a
	ret

Counters:
	ld hl, wFrames
	inc [hl]
	jr nz, .input
	inc hl
	inc [hl]
.input:
	ld a, [wCurJoy]
	or a
	jr z, .lock
	ld hl, wInputFrames
	inc [hl]
	jr nz, .lock
	inc hl
	inc [hl]
.lock:
	ld a, [wLock]
	or a
	ret z
	dec a
	ld [wLock], a
	ret

HandleInput:
	; newly pressed buttons -> b
	ld a, [wPrevJoy]
	cpl
	ld c, a
	ld a, [wCurJoy]
	and c
	ld b, a

	; Start opens dialog, B closes it, A sets flag bit 0
	bit 7, b
	jr z, .noStart
	ld a, 1
	ld [wDialog], a
	call LoadDialogText
	ld a, [wMapId]
	cp 2
	jr nz, .noStart
	ld hl, wMilestones
	set 2, [hl]
.noStart:
	bit 5, b
	jr z, .noB
	xor a
	ld [wDialog], a
.noB:
	bit 4, b
	jr z, .noA
	ld hl, wFlags
	set 0, [hl]
.noA:
	; movement: not while locked or in dialog
	ld a, [wDialog]
	or a
	ret nz
	ld a, [wLock]
	or a
	ret nz
	ld a, [wCurJoy]
	ld b, a
	bit 0, b
	jr nz, .right
	bit 1, b
	jr nz, .left
	bit 2, b
	jr nz, .up
	bit 3, b
	jr nz, .down
	ret
.right:
	ld a, [wX]
	inc a
	cp MAX_COORD + 1
	jr nz, .setX
	ld a, [wMapId]
	cp 1
	jr nz, .rightClamp
	ld a, 2
	ld [wMapId], a
	xor a
	jr .setX
.rightClamp:
	ld a, MAX_COORD
	jr .setX
.left:
	ld a, [wX]
	or a
	jr nz, .leftStep
	ld a, [wMapId]
	cp 2
	ret nz
	ld a, 1
	ld [wMapId], a
	ld a, MAX_COORD
	jr .setX
.leftStep:
	dec a
.setX:
	ld [wX], a
	jr .moved
.up:
	ld a, [wY]
	or a
	ret z
	dec a
	ld [wY], a
	jr .moved
.down:
	ld a, [wY]
	cp MAX_COORD
	ret z
	inc a
	ld [wY], a
.moved:
	ld a, WALK_LOCK
	ld [wLock], a
	ld a, [wMapId]
	cp 2
	ret nz
	ld a, [wParty + 2]          ; lead creature hp drops per step on map 2
	or a
	ret z
	dec a
	ld [wParty + 2], a
	ret

LoadDialogText:
	ld a, [wMapId]
	cp 1
	ld hl, TextMap1
	jr z, .copy
	ld hl, TextMap2
.copy:
	ld de, wDialogText
	ld b, 16
.loop:
	ld a, [hl+]
	ld [de], a
	inc de
	dec b
	jr nz, .loop
	ret

TextMap1: db "HELLO TRAINER@", 0, 0
TextMap2: db "WELCOME@", 0, 0, 0, 0, 0, 0, 0, 0

CheckMilestones:
	ld a, [wMapId]
	cp 1
	jr nz, .map2
	ld a, [wX]
	cp 3
	ret nz
	ld a, [wY]
	cp 3
	ret nz
	ld hl, wMilestones
	set 0, [hl]
	ret
.map2:
	ld hl, wMilestones
	set 1, [hl]
	ld a, [wX]
	cp 5
	jr nz, .row
	ld a, [wY]
	cp 5
	jr nz, .row
	set 3, [hl]
.row:
	ld a, [wY]
	cp MAX_COORD
	ret nz
	inc hl
	set 0, [hl]                 ; milestone bit 8 (second byte)
	ret

; scroll follows the player; palette differs per map so frames change on map change
Present:
	ld a, [wX]
	swap a
	srl a
	ldh [rSCX], a
	ld a, [wY]
	swap a
	srl a
	ldh [rSCY], a
	ld a, [wMapId]
	cp 1
	ld a, $E4
	jr z, .pal
	ld a, $1B
.pal:
	ldh [rBGP], a
	ret
