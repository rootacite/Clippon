/* libevdev name tag: makes each instance's virtual input devices identifiable.
 *
 * Every Sunshine process creates its own keyboard, mice and (later, when a
 * client connects) touch/pen devices. They all carry the same name and the
 * same vendor:product, so udev cannot tell which instance a device belongs to
 * and puts them all on one seat. Every cage then reads every device, and input
 * typed into one instance also reaches the others.
 *
 * libvirtualhid builds those devices with libevdev, and Sunshine links
 * libevdev dynamically, so this shim sees the name first and appends the
 * instance tag from CLIPPON_VHID_TAG (for example "@alpha"). The udev helper
 * clippon-seat-tag turns that tag into a per-instance seat.
 *
 * build: gcc -shared -fPIC -O2 -o libclippon_vhid_name.so vhid_name.c
 * use:   LD_PRELOAD=/usr/local/lib/libclippon_vhid_name.so CLIPPON_VHID_TAG=@alpha sunshine
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>

struct libevdev;

typedef int (*set_name_fn)(struct libevdev *dev, const char *name);

static set_name_fn next_set_name(void) {
	static set_name_fn real;
	if (real == NULL) {
		/* RTLD_NEXT, not RTLD_DEFAULT: the default scope resolves back to
		 * this file's own definition and recurses. */
		real = (set_name_fn)dlsym(RTLD_NEXT, "libevdev_set_name");
	}
	return real;
}

int libevdev_set_name(struct libevdev *dev, const char *name) {
	set_name_fn real = next_set_name();
	if (real == NULL) {
		return -1;
	}
	const char *tag = getenv("CLIPPON_VHID_TAG");
	if (name == NULL || tag == NULL || *tag == '\0') {
		return real(dev, name);
	}
	char tagged[128];
	snprintf(tagged, sizeof(tagged), "%s%s", name, tag);
	return real(dev, tagged);
}
