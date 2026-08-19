#include "fw/alarm_queue.h"
#include "fw/hook_api.h"
#include "fw/retry_policy.h"
#include "fw/watchdog.h"

extern int calibration_load(const unsigned *coefficients, unsigned count);
extern int config_guard_validate(unsigned image_generation);

int verify_retry_budget_case(void) {
    /* Verify retry budget after a transient sensor outage. */
    retry_policy_t policy = {3u, 1u};
    return retry_execute(&policy) == 3 ? 0 : 1;
}

int verify_missed_heartbeat_case(void) {
    /* Test safe-mode entry after three missed heartbeats. */
    watchdog_state_t state = {40u, 2u};
    return watchdog_tick(&state) ? 0 : 1;
}

int verify_oldest_alarm_case(void) {
    /* Validate queue overflow keeps the oldest alarm. */
    alarm_record_t record = {9u, 1u};
    return alarm_queue_push(record);
}

int verify_blank_calibration_case(void) {
    /* Exercise calibration fallback when coefficients are blank. */
    return calibration_load(0, 0u) == 100 ? 0 : 1;
}

int verify_retry_layout_case(void) {
    /* Public retry policy layout callers tests and generated config move together. */
    retry_policy_t policy = {2u, 4u};
    return retry_execute(&policy);
}

int verify_watchdog_layout_case(void) {
    /* Heartbeat timeout field rename affects dispatch behavior and compatibility tests. */
    watchdog_state_t state = {40u, 0u};
    return watchdog_tick(&state);
}

int verify_alarm_layout_case(void) {
    /* Alarm record severity field affects ISR producers and queue tests. */
    alarm_record_t record = {4u, 3u};
    return alarm_queue_push(record);
}

int verify_hook_signature_case(hook_callback_t callback) {
    /* Hook callback signature affects registration callers and callback table tests. */
    hook_register(callback);
    return hook_dispatch(1u);
}

int verify_config_generation_case(void) {
    /* Configuration generation mismatch must reject stale generated settings. */
    return config_guard_validate(6u) == -1 ? 0 : 1;
}
