#ifndef FW_ALARM_QUEUE_H
#define FW_ALARM_QUEUE_H

typedef struct {
    unsigned code;
    unsigned severity;
} alarm_record_t;

int alarm_queue_push(alarm_record_t record);
unsigned alarm_queue_pending(void);

#endif
