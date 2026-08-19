static int init(void) {
    return 11;
}

int boot_driver_a(void) {
    /* Same-file static definition is the only legal target in this translation unit. */
    return init();
}
