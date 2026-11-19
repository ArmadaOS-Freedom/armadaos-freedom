#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#define ARRAY_SIZE(array) (sizeof(array) / sizeof((array)[0]))
#define BIT(index) (1UL << (index))
#define container_of(ptr, type, member) \
	((type *)((char *)(ptr) - offsetof(type, member)))
#define READ_ONCE(value) (value)
#define WRITE_ONCE(value, new_value) ((value) = (new_value))
#define GT9764_CONTROL 0x02
#define GT9764_POSITION 0x03
#define GT9764_AAC_MODE 0x06
#define GT9764_AAC_TIMING 0x07
#define GT9764_POSITION_INIT 512
#define CCI_REG8(reg) (reg)
#define CCI_REG16(reg) (reg)
#define kfree free

typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;

struct regulator_dev { unsigned int disable_failures; };
struct regulator {
	struct regulator_dev *rdev;
	unsigned int enable_count;
	int disable_error;
};
struct ww_acquire_ctx {};
struct regulator_bulk_data { const char *supply; struct regulator *consumer; };
struct device { void *drvdata; };
struct i2c_client {};
struct mutex { bool locked; };
struct kref { unsigned int count; };
struct v4l2_ctrl_handler { struct mutex *lock; };
struct v4l2_ctrl { struct { int val; } cur; };
struct v4l2_subdev { struct device *dev; int entity; };
struct v4l2_subdev_fh {};
struct v4l2_subdev_state {};
struct media_pad {};
struct clk {};
struct gpio_desc {};
struct regmap {};
struct cci_reg_sequence { unsigned int reg; };

static struct device dev;
static struct i2c_client client;
static struct mutex device_lock;
static struct regulator_dev rails[3];
static struct regulator supplies[3];
static struct v4l2_ctrl focus = { .cur.val = GT9764_POSITION_INIT };
static struct v4l2_subdev_state state;
static struct clk clock;
static struct gpio_desc reset;
static struct regmap map;
static int usage, pm_status_error;
static int (*runtime_resume)(struct device *);
static int (*runtime_suspend)(struct device *);
static bool active, disabled, reset_level, clock_on, rear;
static unsigned int elapsed, reset_released, cci_ready, cci_calls, bus_writes;
static unsigned int clock_started, fail_bus_write, fail_asserts, freed_ctrls;
static struct regulator *enable_failure, *disable_failure;
static bool fail_after_release;

static void fsleep(unsigned int delay) { elapsed += delay; }
static void *dev_get_drvdata(struct device *pdev) { return pdev->drvdata; }
static struct i2c_client *v4l2_get_subdevdata(struct v4l2_subdev *sd) { return &client; }
static void dev_err(struct device *pdev, const char *format, ...) {}
#define dev_err_ratelimited dev_err

static void mutex_lock(struct mutex *lock)
{
	assert(!lock->locked);
	lock->locked = true;
}

static void mutex_unlock(struct mutex *lock)
{
	assert(lock->locked);
	lock->locked = false;
}

static void kref_get(struct kref *ref) { assert(ref->count++ > 0); }
static void kref_put(struct kref *ref, void (*release)(struct kref *))
{
	assert(ref->count);
	if (--ref->count == 0)
		release(ref);
}

static void media_entity_cleanup(int *entity) {}
static void v4l2_ctrl_handler_free(struct v4l2_ctrl_handler *handler)
{
	assert(!handler->lock->locked);
	freed_ctrls++;
}

static int regulator_enable(struct regulator *regulator)
{
	if (regulator == enable_failure)
		return -EIO;
	regulator->enable_count++;
	return 0;
}

static void regulator_lock_dependent(struct regulator_dev *rdev,
				     struct ww_acquire_ctx *ctx) {}
static void regulator_unlock_dependent(struct regulator_dev *rdev,
				       struct ww_acquire_ctx *ctx) {}

static int _regulator_disable(struct regulator *regulator)
{
	assert(regulator->enable_count == 1);
	if (regulator == disable_failure && !fail_after_release)
		return -EIO;
	regulator->enable_count--;
	return regulator == disable_failure ? -EAGAIN : 0;
}

static int gpiod_set_value_cansleep(struct gpio_desc *gpio, int asserted)
{
	if (asserted && fail_asserts) {
		fail_asserts--;
		return -EIO;
	}
	reset_level = asserted;
	if (!asserted) {
		assert(clock_on != rear);
		reset_released = elapsed;
		cci_ready = elapsed + (rear ? 16000 : 3000);
	}
	return 0;
}

static int clk_prepare_enable(struct clk *clk)
{
	assert(!clock_on && reset_level != rear);
	if (rear)
		assert(elapsed >= reset_released + 1000);
	clock_on = true;
	clock_started++;
	return 0;
}

static void clk_disable_unprepare(struct clk *clk)
{
	assert(clock_on);
	clock_on = false;
}

static int pm_runtime_resume_and_get(struct device *pdev)
{
	int ret;
	assert(!disabled);
	if (pm_status_error)
		return -EINVAL;
	if (!active) {
		ret = runtime_resume(pdev);
		if (ret) {
			if (ret != -EBUSY && ret != -EAGAIN)
				pm_status_error = ret;
			return ret;
		}
		active = true;
	}
	usage++;
	return 0;
}

static void pm_runtime_put_noidle(struct device *pdev) { assert(usage-- > 0); }
static int pm_runtime_put_sync(struct device *pdev)
{
	int ret;
	assert(!disabled);
	pm_runtime_put_noidle(pdev);
	if (usage)
		return 0;
	ret = runtime_suspend(pdev);
	if (ret && ret != -EBUSY && ret != -EAGAIN)
		pm_status_error = ret;
	active = ret != 0;
	return ret;
}

static void pm_runtime_disable(struct device *pdev) { disabled = true; }
static void pm_runtime_set_suspended(struct device *pdev)
{
	assert(disabled);
	active = false;
}

static int i2c_smbus_write_byte_data(struct i2c_client *bus, u8 reg, u8 value)
{
	assert(supplies[0].enable_count && supplies[1].enable_count);
	return ++bus_writes == fail_bus_write ? -EIO : 0;
}

static int i2c_smbus_write_word_swapped(struct i2c_client *bus, u8 reg, u16 value)
{
	return i2c_smbus_write_byte_data(bus, reg, 0);
}

static struct v4l2_subdev_state *v4l2_subdev_lock_and_get_active_state(struct v4l2_subdev *sd)
{
	mutex_lock(&device_lock);
	return &state;
}

static void v4l2_subdev_unlock_state(struct v4l2_subdev_state *active_state)
{
	assert(active_state == &state);
	mutex_unlock(&device_lock);
}

static int cci_write(struct regmap *regmap, unsigned int reg, int value, int *error)
{
	if (error && *error)
		return *error;
	assert(clock_on && !reset_level && elapsed >= cci_ready);
	assert(supplies[0].enable_count && supplies[1].enable_count && supplies[2].enable_count);
	if (++cci_calls == fail_bus_write) {
		if (error)
			*error = -EIO;
		return -EIO;
	}
	return 0;
}

static void cci_multi_reg_write(struct regmap *regmap, const struct cci_reg_sequence *regs,
				int num, int *error)
{
	cci_write(regmap, 0, 0, error);
}

static int __v4l2_ctrl_handler_setup(struct v4l2_ctrl_handler *handler) { return 0; }
static const struct cci_reg_sequence gc08a8_init_regs[1], gc08a8_3264x2448_regs[1];
static const struct cci_reg_sequence s5kjns_reset_regs[1], s5kjns_init_regs[1];
static const struct cci_reg_sequence s5kjns_4080x3072_regs[1];

/* FUNCTIONS */

static void fresh(struct v4l2_subdev *sd, struct regulator_bulk_data *bulk, unsigned int count)
{
	dev.drvdata = sd;
	sd->dev = &dev;
	usage = pm_status_error = elapsed = cci_ready = cci_calls = bus_writes = clock_started = 0;
	reset_released = fail_bus_write = fail_asserts = freed_ctrls = 0;
	active = disabled = clock_on = fail_after_release = false;
	reset_level = true;
	enable_failure = disable_failure = NULL;
	for (unsigned int index = 0; index < count; index++) {
		rails[index] = (struct regulator_dev){0};
		supplies[index] = (struct regulator){ .rdev = &rails[index] };
		bulk[index] = (struct regulator_bulk_data){ "test", &supplies[index] };
	}
}

#if defined(TEST_LENS)
static struct gt9764 *fresh_lens(void)
{
	struct gt9764 *lens = calloc(1, sizeof(*lens));
	assert(lens);
	lens->ref.count = 1;
	lens->ctrls.lock = &device_lock;
	lens->focus = &focus;
	fresh(&lens->sd, lens->supplies, ARRAY_SIZE(lens->supplies));
	runtime_resume = gt9764_runtime_resume;
	runtime_suspend = gt9764_power_off;
	return lens;
}

int main(void)
{
	struct gt9764 *lens = fresh_lens();
	fail_bus_write = 1;
	assert(gt9764_open(&lens->sd, NULL) == -EAGAIN);
	assert(!lens->supplies_enabled && !usage && !pm_status_error);
	assert(!gt9764_open(&lens->sd, NULL) && usage == 1 && bus_writes > 1);
	assert(!gt9764_close(&lens->sd, NULL) && !usage);
	gt9764_put(lens);
	puts("PASS initialization failure allows another open after clean rollback");

	lens = fresh_lens();
	assert(!gt9764_open(&lens->sd, NULL));
	disable_failure = &supplies[1];
	assert(gt9764_close(&lens->sd, NULL) == -EIO);
	assert(lens->supplies_enabled == BIT(1) && rails[1].disable_failures == 1);
	disable_failure = NULL;
	gt9764_stop_pm(lens);
	assert(!lens->supplies_enabled && !supplies[1].enable_count && !rails[1].disable_failures);
	gt9764_put(lens);
	puts("PASS detach retries a disable that retained the consumer reference");

	lens = fresh_lens();
	assert(!gt9764_open(&lens->sd, NULL));
	disable_failure = &supplies[1];
	fail_after_release = true;
	assert(gt9764_close(&lens->sd, NULL) == -EAGAIN);
	assert(!lens->supplies_enabled && !supplies[1].enable_count && !usage);
	assert(gt9764_open(&lens->sd, NULL) == -EAGAIN);
	assert(!usage && !lens->open_count && rails[1].disable_failures == 1);
	gt9764_stop_pm(lens);
	gt9764_put(lens);
	puts("PASS post-release fault rejects an active PM get without a double disable");

	lens = fresh_lens();
	gt9764_devnode_get(&lens->sd);
	gt9764_devnode_put(&lens->sd);
	assert(!freed_ctrls);
	gt9764_devnode_get(&lens->sd);
	assert(!gt9764_open(&lens->sd, NULL));
	assert(!gt9764_open(&lens->sd, NULL) && usage == 2);
	gt9764_stop_pm(lens);
	gt9764_put(lens);
	assert(!usage && !lens->open_count && !freed_ctrls);
	assert(!gt9764_close(&lens->sd, NULL) && !gt9764_close(&lens->sd, NULL));
	gt9764_devnode_put(&lens->sd);
	assert(freed_ctrls == 1 && !usage);
	puts("PASS independent node references preserve controls until the last close");
	return 0;
}
#else
static void fresh_sensor(struct SENSOR *sensor)
{
	*sensor = (struct SENSOR){ .xclk = &clock, .reset = &reset, .regmap = &map };
	sensor->ctrls.lock = &device_lock;
	fresh(&sensor->sd, sensor->supplies, ARRAY_SIZE(sensor->supplies));
	runtime_resume = SENSOR_RESUME;
	runtime_suspend = SENSOR_OFF;
	rear = SENSOR_IS_REAR;
}

int main(void)
{
	struct SENSOR sensor;
	fresh_sensor(&sensor);
	enable_failure = &supplies[1];
	assert(SENSOR_STREAM(&sensor.sd, 1) == -EAGAIN);
	assert(!sensor.supplies_enabled && !usage && !pm_status_error && !cci_calls);
	enable_failure = NULL;
	assert(!SENSOR_STREAM(&sensor.sd, 1) && sensor.streaming && usage == 1);
	assert(!SENSOR_STREAM(&sensor.sd, 0) && !usage && !active);
	puts("PASS a transient regulator enable error permits a subsequent stream");

	fresh_sensor(&sensor);
	fail_bus_write = 1;
	assert(!pm_runtime_resume_and_get(&dev));
	assert(SENSOR_STREAM(&sensor.sd, 1) == -EIO && !sensor.streaming);
	assert(reset_level && usage == 1 && active);
	assert(!SENSOR_STREAM(&sensor.sd, 1) && !reset_level && usage == 2);
	assert(clock_started == (rear ? 2 : 1));
	assert(!SENSOR_STREAM(&sensor.sd, 0) && usage == 1);
	assert(!pm_runtime_put_sync(&dev) && !usage);
	puts("PASS start retry releases reset and waits with an external PM hold");

	fresh_sensor(&sensor);
	fail_bus_write = 1;
	assert(!pm_runtime_resume_and_get(&dev));
	fail_asserts = 1;
	assert(SENSOR_STREAM(&sensor.sd, 1) == -EIO && sensor.reset_error == -EIO);
	assert(!pm_runtime_put_sync(&dev));
	assert(!sensor.reset_error && !sensor.power_error && !usage);
	assert(!SENSOR_STREAM(&sensor.sd, 1));
	assert(!SENSOR_STREAM(&sensor.sd, 0));
	puts("PASS successful power-off reset clears the earlier GPIO error");

	fresh_sensor(&sensor);
	assert(!pm_runtime_resume_and_get(&dev));
	disable_failure = &supplies[2];
	fail_after_release = true;
	assert(pm_runtime_put_sync(&dev) == -EAGAIN && active && !usage);
	assert(SENSOR_STREAM(&sensor.sd, 1) == -EAGAIN && !usage && !cci_calls);
	assert(!sensor.streaming && !sensor.supplies_enabled);
	puts("PASS a PM get racing failed suspend cannot reach sensor I/O");
	return 0;
}
#endif
