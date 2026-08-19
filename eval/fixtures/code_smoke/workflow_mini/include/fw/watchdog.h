#ifndef FW_WATCHDOG_H
#define FW_WATCHDOG_H

typedef struct {
    unsigned heartbeat_timeout_ticks;
    unsigned missed_heartbeats;
} watchdog_state_t;

void watchdog_note_heartbeat(watchdog_state_t *state);
int watchdog_tick(watchdog_state_t *state);

#endif
