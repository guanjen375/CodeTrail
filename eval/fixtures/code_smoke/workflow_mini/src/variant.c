#if defined(BOARD_ALPHA)
int variant_init(void) {
    return 1;
}
#else
int variant_init(void) {
    return 2;
}
#endif

int variant_boot(void) {
    /* Without a selected build variant this call cannot be proven unique. */
    return variant_init();
}
