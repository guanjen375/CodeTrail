static int init(void) {
    return 22;
}

int boot_driver_b(void) {
    /* Duplicate static names in another translation unit must remain local. */
    return init();
}
