/* Map every libinput pointer onto the compositor's output, and hide the
   cursor while a touchscreen is the input in use.

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

   Touch is a second device. cage forwards it as wl_touch and never moves the
   pointer, but it also never hides the pointer image, and
   WLR_NO_HARDWARE_CURSORS paints that image into the frame. A touch client
   therefore sees a cursor sitting wherever the mouse last was. KWin hides the
   cursor for the whole time touch is the latest input and shows it again on
   the next pointer event. The same rule is applied here: a touch coordinate
   conversion hides the image, and a pointer move, click or scroll puts back
   the image cage or the client asked for.

   build: gcc -shared -fPIC -O2 -o libwlr_cursor_map.so wlr_cursor_map.c
   use:   LD_PRELOAD=/usr/local/lib/libwlr_cursor_map.so cage ...
*/
#define _GNU_SOURCE

#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>

/* Only the leading fields are needed; the SDK header is an unstable interface
   that is not installed on every system. The values match
   enum wlr_input_device_type in wlroots 0.20. */
struct wlr_input_device {
  int type;
  char *name; /* may be NULL */
};

enum {
  DEVICE_POINTER = 1,
  DEVICE_TOUCH = 2
};

struct wlr_cursor;
struct wlr_output;
struct wlr_output_layout;
struct wlr_xcursor_manager;
struct wlr_surface;
struct wlr_seat;

typedef void (*attach_fn)(struct wlr_cursor *, struct wlr_input_device *);
typedef void (*layout_fn)(struct wlr_cursor *, struct wlr_output_layout *);
typedef void (*map_fn)(struct wlr_cursor *, struct wlr_input_device *,
                       struct wlr_output *);
typedef struct wlr_output *(*center_fn)(struct wlr_output_layout *);
typedef void (*unset_fn)(struct wlr_cursor *);
typedef void (*xcursor_fn)(struct wlr_cursor *, struct wlr_xcursor_manager *,
                           const char *);
typedef void (*surface_fn)(struct wlr_cursor *, struct wlr_surface *, int32_t,
                           int32_t);
typedef void (*move_fn)(struct wlr_cursor *, struct wlr_input_device *, double,
                        double);
typedef void (*warp_abs_fn)(struct wlr_cursor *, struct wlr_input_device *,
                            double, double);
typedef void (*abs_coords_fn)(struct wlr_cursor *, struct wlr_input_device *,
                              double, double, double *, double *);
typedef uint32_t (*button_fn)(struct wlr_seat *, uint32_t, uint32_t, uint32_t);
typedef void (*axis_fn)(struct wlr_seat *, uint32_t, int, double, int32_t, int,
                        int);

enum { IMAGE_NONE = 0, IMAGE_XCURSOR = 1, IMAGE_SURFACE = 2 };

static struct wlr_output_layout *cursor_layout;
static struct wlr_cursor *image_cursor;
static struct wlr_xcursor_manager *xcursor_manager;
static const char *xcursor_name;
static struct wlr_surface *cursor_surface;
static int32_t hotspot_x;
static int32_t hotspot_y;
static int image_kind;
static int hidden_for_touch;

/* RTLD_NEXT, not RTLD_DEFAULT: this library defines some of the symbols being
   looked up, and RTLD_DEFAULT would hand back our own definition, which is an
   infinite recursion the compiler turns into a loop. */
static void *next_symbol(const char *name) {
  return dlsym(RTLD_NEXT, name);
}

static unset_fn real_unset(void) {
  static unset_fn fn;
  if (!fn) {
    fn = (unset_fn) next_symbol("wlr_cursor_unset_image");
  }
  return fn;
}

static xcursor_fn real_xcursor(void) {
  static xcursor_fn fn;
  if (!fn) {
    fn = (xcursor_fn) next_symbol("wlr_cursor_set_xcursor");
  }
  return fn;
}

static surface_fn real_surface(void) {
  static surface_fn fn;
  if (!fn) {
    fn = (surface_fn) next_symbol("wlr_cursor_set_surface");
  }
  return fn;
}

static void apply_image(void) {
  if (!image_cursor) {
    return;
  }
  if (image_kind == IMAGE_SURFACE && real_surface()) {
    real_surface()(image_cursor, cursor_surface, hotspot_x, hotspot_y);
  } else if (image_kind == IMAGE_XCURSOR && real_xcursor()) {
    real_xcursor()(image_cursor, xcursor_manager, xcursor_name);
  }
}

static void hide_cursor(void) {
  unset_fn unset = real_unset();
  if (hidden_for_touch) {
    return;
  }
  hidden_for_touch = 1;
  if (image_cursor && unset) {
    unset(image_cursor);
  }
  fprintf(stderr, "[clippon-cursor] hiding the cursor while touch is in use\n");
}

static void show_cursor(void) {
  if (!hidden_for_touch) {
    return;
  }
  hidden_for_touch = 0;
  apply_image();
  fprintf(stderr, "[clippon-cursor] showing the cursor for pointer input\n");
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

void wlr_cursor_set_xcursor(struct wlr_cursor *cursor,
                            struct wlr_xcursor_manager *manager,
                            const char *name) {
  image_cursor = cursor;
  xcursor_manager = manager;
  xcursor_name = name;
  image_kind = IMAGE_XCURSOR;
  if (!hidden_for_touch && real_xcursor()) {
    real_xcursor()(cursor, manager, name);
  }
}

void wlr_cursor_set_surface(struct wlr_cursor *cursor,
                            struct wlr_surface *surface, int32_t hot_x,
                            int32_t hot_y) {
  image_cursor = cursor;
  cursor_surface = surface;
  hotspot_x = hot_x;
  hotspot_y = hot_y;
  image_kind = IMAGE_SURFACE;
  if (!hidden_for_touch && real_surface()) {
    real_surface()(cursor, surface, hot_x, hot_y);
  }
}

void wlr_cursor_unset_image(struct wlr_cursor *cursor) {
  image_cursor = cursor;
  image_kind = IMAGE_NONE;
  hidden_for_touch = 0;
  if (real_unset()) {
    real_unset()(cursor);
  }
}

void wlr_cursor_absolute_to_layout_coords(struct wlr_cursor *cursor,
                                          struct wlr_input_device *device,
                                          double x, double y, double *lx,
                                          double *ly) {
  static abs_coords_fn real;
  if (!real) {
    real = (abs_coords_fn) next_symbol("wlr_cursor_absolute_to_layout_coords");
  }
  if (device && device->type == DEVICE_TOUCH) {
    hide_cursor();
  }
  if (real) {
    real(cursor, device, x, y, lx, ly);
  }
}

void wlr_cursor_move(struct wlr_cursor *cursor, struct wlr_input_device *device,
                     double delta_x, double delta_y) {
  static move_fn real;
  if (!real) {
    real = (move_fn) next_symbol("wlr_cursor_move");
  }
  show_cursor();
  if (real) {
    real(cursor, device, delta_x, delta_y);
  }
}

void wlr_cursor_warp_absolute(struct wlr_cursor *cursor,
                              struct wlr_input_device *device, double x,
                              double y) {
  static warp_abs_fn real;
  if (!real) {
    real = (warp_abs_fn) next_symbol("wlr_cursor_warp_absolute");
  }
  show_cursor();
  if (real) {
    real(cursor, device, x, y);
  }
}

uint32_t wlr_seat_pointer_notify_button(struct wlr_seat *seat, uint32_t time,
                                        uint32_t button, uint32_t state) {
  static button_fn real;
  if (!real) {
    real = (button_fn) next_symbol("wlr_seat_pointer_notify_button");
  }
  show_cursor();
  if (!real) {
    return 0;
  }
  return real(seat, time, button, state);
}

void wlr_seat_pointer_notify_axis(struct wlr_seat *seat, uint32_t time,
                                  int orientation, double value,
                                  int32_t discrete, int source, int direction) {
  static axis_fn real;
  if (!real) {
    real = (axis_fn) next_symbol("wlr_seat_pointer_notify_axis");
  }
  show_cursor();
  if (real) {
    real(seat, time, orientation, value, discrete, source, direction);
  }
}
