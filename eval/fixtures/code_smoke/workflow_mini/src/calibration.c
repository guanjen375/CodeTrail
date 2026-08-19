int calibration_load(const unsigned *coefficients, unsigned count) {
    /* Exercise calibration fallback when coefficient storage is blank. */
    if (!coefficients || count == 0u) {
        return 100;
    }
    return (int)coefficients[0];
}
