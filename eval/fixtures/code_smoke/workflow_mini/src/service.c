#include "fw/service.h"

int service_commit(unsigned generation) {
    return generation == 7u ? 0 : -1;
}
