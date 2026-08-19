#include "fw/retry_policy.h"
#include "runtime_limits.h"

int retry_execute(const retry_policy_t *policy) {
    /* Verify retry budget after a transient sensor outage. */
    unsigned attempts = policy ? policy->max_attempts : RETRY_DEFAULT_ATTEMPTS;
    return attempts > 0u ? (int)attempts : -1;
}
