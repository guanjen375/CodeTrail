#ifndef FW_RETRY_POLICY_H
#define FW_RETRY_POLICY_H

typedef struct {
    unsigned max_attempts;
    unsigned backoff_ticks;
} retry_policy_t;

int retry_execute(const retry_policy_t *policy);

#endif
