#include "state_machine.h"

#define SM_CALLBACK_SLOTS 8u

static sm_state_t current_state;
static sm_event_callback_t callback_table[SM_CALLBACK_SLOTS];

void sm_init(void) {
    uint32_t i;
    current_state = SM_STATE_IDLE;
    for (i = 0u; i < SM_CALLBACK_SLOTS; i++) {
        callback_table[i] = 0;
    }
}

sm_state_t sm_current_state(void) {
    return current_state;
}

void sm_register_callback(event_id_t id, sm_event_callback_t cb) {
    if ((uint32_t)id < SM_CALLBACK_SLOTS) {
        callback_table[id] = cb;
    }
}

static sm_state_t sm_transition(
    sm_state_t from,
    const event_t *evt)
{
    if (evt->id == EVT_COMM_RX && evt->payload == 0u) {
        return SM_STATE_FAULT;
    }
    if (from == SM_STATE_IDLE && evt->id == EVT_BUTTON_PRESS) {
        return SM_STATE_ACTIVE;
    }
    if (from == SM_STATE_ACTIVE && evt->id == EVT_TIMER_TICK) {
        return SM_STATE_IDLE;
    }
    return from;
}

void sm_handle_event(const event_t *evt) {
    sm_event_callback_t cb;
    current_state = sm_transition(current_state, evt);
    cb = callback_table[evt->id];
    if (cb != 0) {
        cb(evt);
    }
}
