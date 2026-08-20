/* Interrupt vector table for the mini firmware image.
 * Level 1 of the file-kind policy makes this file grep-discoverable;
 * symbol extraction for assembly is deliberately deferred to Level 2.
 */
    .syntax unified
    .section .isr_vector, "a"
    .global __isr_vector_table
__isr_vector_table:
    .word _estack
    .word reset_handler
    .word fault_handler

    .section .text
    .global reset_handler
reset_handler:
    bl  app_bootstrap
    b   .
