#include "fw/watchdog.h"
#include "runtime_limits.h"

void watchdog_note_heartbeat(watchdog_state_t *state) {
    state->missed_heartbeats = 0u;
}

int watchdog_tick(watchdog_state_t *state) {
    /* Safe-mode entry follows three missed heartbeats; FAULT_WDOG_EXPIRED is upstream. */
    state->missed_heartbeats += 1u;
    return state->missed_heartbeats >= 3u ||
           state->heartbeat_timeout_ticks > HEARTBEAT_TIMEOUT_TICKS;
}
