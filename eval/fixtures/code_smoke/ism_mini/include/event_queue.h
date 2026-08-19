#ifndef ISM_EVENT_QUEUE_H
#define ISM_EVENT_QUEUE_H

#include <stdint.h>

#define EVENT_QUEUE_CAPACITY 16u

typedef enum {
    EVT_NONE = 0,
    EVT_TIMER_TICK,
    EVT_BUTTON_PRESS,
    EVT_COMM_RX,
} event_id_t;

typedef struct {
    event_id_t id;
    uint32_t payload;
} event_t;

void event_queue_init(void);
int event_queue_push(event_t evt);
int event_queue_pop(event_t *out);
uint32_t event_queue_dropped_count(void);
void event_queue_reset_statistics(
    uint32_t *dropped_out,
    uint32_t *high_watermark_out);

#endif /* ISM_EVENT_QUEUE_H */
