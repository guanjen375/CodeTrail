#include <fw/service.h>

int service_user_flush(unsigned generation) {
    /* Repo-owned angle header makes the external declaration visible. */
    return service_commit(generation);
}
