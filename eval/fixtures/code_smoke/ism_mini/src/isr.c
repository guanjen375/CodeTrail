#include "event_queue.h"

static volatile uint32_t tick_count;

void timer_isr(void) {
    event_t evt;
    tick_count++;
    evt.id = EVT_TIMER_TICK;
    evt.payload = tick_count;
    event_queue_push(evt);
}

void button_isr(uint32_t button_mask) {
    event_t evt;
    evt.id = EVT_BUTTON_PRESS;
    evt.payload = button_mask;
    event_queue_push(evt);
}
