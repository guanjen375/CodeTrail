#include "event_queue.h"

static event_t queue_slots[EVENT_QUEUE_CAPACITY];
static uint32_t queue_head;
static uint32_t queue_tail;
static uint32_t queue_dropped;
static uint32_t queue_high_watermark;

void event_queue_init(void) {
    queue_head = 0u;
    queue_tail = 0u;
    queue_dropped = 0u;
    queue_high_watermark = 0u;
}

int event_queue_push(event_t evt) {
    uint32_t next = (queue_tail + 1u) % EVENT_QUEUE_CAPACITY;
    uint32_t depth;
    if (next == queue_head) {
        queue_dropped++;
        return -1;
    }
    queue_slots[queue_tail] = evt;
    queue_tail = next;
    depth = (queue_tail + EVENT_QUEUE_CAPACITY - queue_head) % EVENT_QUEUE_CAPACITY;
    if (depth > queue_high_watermark) {
        queue_high_watermark = depth;
    }
    return 0;
}

int event_queue_pop(event_t *out) {
    if (queue_head == queue_tail) {
        return -1;
    }
    *out = queue_slots[queue_head];
    queue_head = (queue_head + 1u) % EVENT_QUEUE_CAPACITY;
    return 0;
}

uint32_t event_queue_dropped_count(void) {
    return queue_dropped;
}

void event_queue_reset_statistics(
    uint32_t *dropped_out,
    uint32_t *high_watermark_out)
{
    if (dropped_out != 0) {
        *dropped_out = queue_dropped;
    }
    if (high_watermark_out != 0) {
        *high_watermark_out = queue_high_watermark;
    }
    queue_dropped = 0u;
    queue_high_watermark = 0u;
}
