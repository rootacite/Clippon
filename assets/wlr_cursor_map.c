/* Map every libinput pointer onto the compositor's output.

   cage 0.3.1 maps a pointer only when wlr_pointer.output_name is set, and
   wlroots 0.20.2 never fills that field from the udev WL_OUTPUT property, so
   Sunshine's virtual mice stay unmapped:

     Input device libvirtualhid Mouse cannot be mapped to an output device

   With no mapping, wlr_cursor_absolute_to_layout_coords() keeps returning the
   unchanged device coordinates, so an absolute pointer event (what Moonlight
   sends in desktop mode) leaves the cursor where it was.

   wlr_cursor_attach_input_device is the one point every device passes through
   and it is wlroots calling it rather than cage, so cage's output_name check
   is not in the way. The layout pointer is captured from
   wlr_cursor_attach_output_layout because struct wlr_cursor is opaque.

   build: gcc -shared -fPIC -O2 -o libwlr_cursor_map.so wlr_cursor_map.c
   use:   LD_PRELOAD=/usr/local/lib/libwlr_cursor_map.so cage ...
*/
#define _GNU_SOURCE

#include <dlfcn.h>
#include <stdio.h>

/* Only the leading fields are needed; the SDK header is an unstable interface
   that is not installed on every system. */
struct wlr_input_device {
  int type;
  char *name; /* may be NULL */
};

struct wlr_cursor;
struct wlr_output;
struct wlr_output_layout;

typedef void (*attach_fn)(struct wlr_cursor *, struct wlr_input_device *);
typedef void (*layout_fn)(struct wlr_cursor *, struct wlr_output_layout *);
typedef void (*map_fn)(struct wlr_cursor *, struct wlr_input_device *,
                       struct wlr_output *);
typedef struct wlr_output *(*center_fn)(struct wlr_output_layout *);

static struct wlr_output_layout *cursor_layout;

/* RTLD_NEXT, not RTLD_DEFAULT: this library defines some of the symbols being
   looked up, and RTLD_DEFAULT would hand back our own definition, which is an
   infinite recursion the compiler turns into a loop. */
static void *next_symbol(const char *name) {
  return dlsym(RTLD_NEXT, name);
}

void wlr_cursor_attach_output_layout(struct wlr_cursor *cursor,
                                     struct wlr_output_layout *layout) {
  static layout_fn real;
  if (!real) {
    real = (layout_fn) next_symbol("wlr_cursor_attach_output_layout");
  }
  cursor_layout = layout;
  if (real) {
    real(cursor, layout);
  }
}

void wlr_cursor_attach_input_device(struct wlr_cursor *cursor,
                                    struct wlr_input_device *device) {
  static attach_fn real_attach;
  static map_fn real_map;
  static center_fn real_center;
  struct wlr_output *output;

  if (!real_attach) {
    real_attach = (attach_fn) next_symbol("wlr_cursor_attach_input_device");
    real_map = (map_fn) next_symbol("wlr_cursor_map_input_to_output");
    real_center = (center_fn) next_symbol("wlr_output_layout_get_center_output");
  }
  if (!real_attach) {
    return;
  }
  real_attach(cursor, device);

  if (!real_map || !real_center || !cursor_layout) {
    return;
  }
  output = real_center(cursor_layout);
  if (!output) {
    return;
  }
  real_map(cursor, device, output);
  fprintf(stderr, "[clippon-cursor] mapped %s to an output\n",
          device->name ? device->name : "(unnamed)");
}
