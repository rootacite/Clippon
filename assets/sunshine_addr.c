/* getsockname interposer for Sunshine inside a clippon network namespace.

   DNAT rewrites the destination to 10.200.0.N before Sunshine accepts the
   socket, so local_endpoint() would advertise that address in sessionUrl0.
   Moonlight then dials an address it cannot route. SO_ORIGINAL_DST is the
   pre-DNAT destination on a redirected TCP socket; when it differs, report
   that instead. UDP sockets Sunshine binds itself do not have this option,
   and the call falls through to the real getsockname.
*/
#define _GNU_SOURCE

#include <dlfcn.h>
#include <errno.h>
#include <netinet/in.h>
#include <string.h>
#include <sys/socket.h>

#ifndef SO_ORIGINAL_DST
#define SO_ORIGINAL_DST 80
#endif

int getsockname(int fd, struct sockaddr *addr, socklen_t *addrlen) {
  static int (*real_getsockname)(int, struct sockaddr *, socklen_t *) = 0;
  struct sockaddr_in original;
  socklen_t original_len = sizeof(original);
  struct sockaddr_in *current;

  if (!real_getsockname) {
    real_getsockname = (int (*)(int, struct sockaddr *, socklen_t *)) dlsym(
      RTLD_NEXT, "getsockname");
  }
  if (!real_getsockname) {
    errno = ENOSYS;
    return -1;
  }

  int rc = real_getsockname(fd, addr, addrlen);
  if (rc != 0 || !addr || !addrlen || addr->sa_family != AF_INET) {
    return rc;
  }
  if (*addrlen < (socklen_t) sizeof(original)) {
    return rc;
  }
  memset(&original, 0, sizeof(original));
  if (getsockopt(fd, SOL_IP, SO_ORIGINAL_DST, &original, &original_len) != 0) {
    return rc;
  }
  if (original.sin_family != AF_INET || original.sin_addr.s_addr == 0 || original.sin_port == 0) {
    return rc;
  }
  current = (struct sockaddr_in *) addr;
  if (current->sin_addr.s_addr == original.sin_addr.s_addr &&
      current->sin_port == original.sin_port) {
    return rc;
  }
  memcpy(addr, &original, sizeof(original));
  *addrlen = sizeof(original);
  return rc;
}
