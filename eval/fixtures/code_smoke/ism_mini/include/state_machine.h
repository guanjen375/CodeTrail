#ifndef ISM_STATE_MACHINE_H
#define ISM_STATE_MACHINE_H

#include "event_queue.h"

typedef enum {
    SM_STATE_IDLE = 0,
    SM_STATE_ACTIVE,
    SM_STATE_FAULT,
} sm_state_t;

typedef void (*sm_event_callback_t)(const event_t *evt);

void sm_init(void);
sm_state_t sm_current_state(void);
void sm_register_callback(event_id_t id, sm_event_callback_t cb);
void sm_handle_event(const event_t *evt);

#endif /* ISM_STATE_MACHINE_H */
