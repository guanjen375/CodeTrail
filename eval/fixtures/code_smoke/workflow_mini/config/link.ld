/* Linker layout for the mini firmware image. */
ENTRY(reset_handler)

MEMORY
{
    FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 512K
    SRAM  (rwx) : ORIGIN = 0x20000000, LENGTH = 128K
}

SECTIONS
{
    .isr_vector : { KEEP(*(.isr_vector)) } > FLASH
    .text       : { *(.text*) }           > FLASH
    _estack = ORIGIN(SRAM) + LENGTH(SRAM);
}
