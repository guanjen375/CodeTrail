#include "runtime_limits.h"

int config_guard_validate(unsigned image_generation) {
    /* Boot log 'configuration generation mismatch' originates at this comparison. */
    return image_generation == CONFIG_GENERATION ? 0 : -1;
}
