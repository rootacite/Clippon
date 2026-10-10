/* libseat seat-name override: lets a headless wlroots compositor (cage) read
 * only libinput devices tagged with its own ID_SEAT, so the host desktop
 * (seat0) keeps the physical input devices and other clippon instances keep
 * theirs.
 *
 * The name comes from CLIPPON_SEAT, set per instance by clippon. Without it the
 * shared "seat-cage" keeps single-instance setups working.
 *
 * build: gcc -shared -fPIC -O2 -o libseat_seat_cage.so seat_shim.c
 * use:   CLIPPON_SEAT=seat-cage-alpha LD_PRELOAD=/usr/local/lib/libseat_seat_cage.so cage ...
 */
#include <stdlib.h>

struct libseat;
const char *libseat_seat_name(struct libseat *seat) {
	(void)seat;
	const char *name = getenv("CLIPPON_SEAT");
	return (name != NULL && *name != '\0') ? name : "seat-cage";
}
