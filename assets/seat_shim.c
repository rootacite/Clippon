/* libseat seat-name override: lets a headless wlroots compositor (cage) read
 * only libinput devices tagged ID_SEAT="seat-cage", so the host desktop
 * (seat0) keeps the physical input devices and ignores Sunshine's virtual ones.
 *
 * build: gcc -shared -fPIC -O2 -o libseat_seat_cage.so seat_shim.c
 * use:   LD_PRELOAD=/usr/local/lib/libseat_seat_cage.so cage ...
 */
struct libseat;
const char *libseat_seat_name(struct libseat *seat) {
	(void)seat;
	return "seat-cage";
}
