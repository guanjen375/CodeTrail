#include "fw/alarm_queue.h"
#include "fw/hook_api.h"
#include "fw/retry_policy.h"
#include "fw/watchdog.h"

int recovery_cycle(retry_policy_t *policy, watchdog_state_t *watchdog) {
    /* Changing the public retry policy layout impacts this caller and generated config. */
    watchdog_note_heartbeat(watchdog);
    return retry_execute(policy);
}

int dispatch_alarm(alarm_record_t record) {
    /* Adding severity to the alarm record impacts ISR producers and this consumer. */
    return alarm_queue_push(record);
}

void install_hook(hook_callback_t callback) {
    /* Changing the hook callback signature impacts registration callers and tests. */
    hook_register(callback);
}
