#include "variant_config.h"

#ifdef VARIANT_PRO
uint32_t variant_max_channels(void) {
    return 8u;
}
#else
uint32_t variant_max_channels(void) {
    return 2u;
}
#endif

uint32_t variant_default_baud(void) {
#ifdef VARIANT_PRO
    return 921600u;
#else
    return 115200u;
#endif
}
