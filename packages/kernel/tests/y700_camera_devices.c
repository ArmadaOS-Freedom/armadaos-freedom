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

struct regulator_dev;
struct regulator_ops { int (*is_enabled)(struct regulator_dev *rdev); };
struct regulator_desc { const struct regulator_ops *ops; };
struct regulator_dev {
	const struct regulator_desc *desc;
	bool ena_pin, disable_state_lost;
	unsigned int disable_failures;
	int status;
};
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
struct work_struct { void (*run)(struct work_struct *work); };
struct delayed_work { struct work_struct work; bool pending, disabled; };

#define to_delayed_work(item) container_of(item, struct delayed_work, work)
#define INIT_DELAYED_WORK(item, callback) ((item)->work.run = (callback))
#define msecs_to_jiffies(delay) (delay)
#define system_freezable_wq NULL

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
static unsigned int clock_started, clock_enabled_at, fail_bus_write, fail_asserts, freed_ctrls;
static struct regulator *enable_failure, *disable_failure, *retained_failure;
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

static int regulator_check_disable_fault(struct regulator *regulator);

static int regulator_enable(struct regulator *regulator)
{
	int ret = regulator_check_disable_fault(regulator);

	if (ret)
		return ret;
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
	if (regulator == retained_failure || (regulator == disable_failure && !fail_after_release))
		return -EIO;
	regulator->enable_count--;
	return regulator == disable_failure ? -EAGAIN : 0;
}

static int _regulator_is_enabled(struct regulator_dev *rdev) { return rdev->status; }

static const struct regulator_ops test_regulator_ops = { .is_enabled = _regulator_is_enabled };
static const struct regulator_desc test_regulator = { .ops = &test_regulator_ops };

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
		cci_ready = elapsed + 3000;
	}
	return 0;
}

static int clk_prepare_enable(struct clk *clk)
{
	assert(!clock_on && reset_level != rear);
	if (rear)
		assert(elapsed >= reset_released + 1000);
	clock_on = true;
	clock_enabled_at = elapsed;
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

static int pm_runtime_suspend(struct device *pdev)
{
	int ret;
	if (disabled)
		return -EACCES;
	if (pm_status_error)
		return -EINVAL;
	if (usage)
		return -EAGAIN;
	if (!active)
		return 1;
	ret = runtime_suspend(pdev);
	if (ret && ret != -EBUSY && ret != -EAGAIN)
		pm_status_error = ret;
	active = ret != 0;
	return ret;
}

static int pm_runtime_put_sync(struct device *pdev)
{
	assert(!disabled);
	pm_runtime_put_noidle(pdev);
	return usage ? 0 : pm_runtime_suspend(pdev);
}

static void pm_runtime_disable(struct device *pdev) { disabled = true; }
static void pm_runtime_set_suspended(struct device *pdev)
{
	assert(disabled);
	active = false;
}

static int pm_runtime_force_suspend(struct device *pdev)
{
	int ret;

	assert(!disabled && !usage);
	disabled = true;
	if (!active)
		return 0;
	ret = runtime_suspend(pdev);
	disabled = ret == 0;
	active = ret != 0;
	return ret;
}

static int pm_runtime_force_resume(struct device *pdev)
{
	assert(disabled && !active && !usage);
	disabled = false;
	return 0;
}

static bool queue_delayed_work(void *queue, struct delayed_work *work, unsigned int delay)
{
	assert(delay == 100 && work->work.run);
	if (work->disabled || work->pending)
		return false;
	work->pending = true;
	return true;
}

static void cancel_delayed_work(struct delayed_work *work) { work->pending = false; }

static void disable_delayed_work_sync(struct delayed_work *work)
{
	work->disabled = true;
	cancel_delayed_work(work);
}

static void run_power_off_work(struct delayed_work *work)
{
	assert(work->pending && !work->disabled);
	work->pending = false;
	work->work.run(&work->work);
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
	if (rear)
		assert(elapsed >= clock_enabled_at + 15000);
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
	reset_released = clock_enabled_at = fail_bus_write = fail_asserts = freed_ctrls = 0;
	active = disabled = clock_on = fail_after_release = false;
	reset_level = true;
	enable_failure = disable_failure = retained_failure = NULL;
	for (unsigned int index = 0; index < count; index++) {
		rails[index] = (struct regulator_dev){ .desc = &test_regulator, .status = 1 };
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
	INIT_DELAYED_WORK(&lens->power_off_work, gt9764_retry_power_off);
	fresh(&lens->sd, lens->supplies, ARRAY_SIZE(lens->supplies));
	runtime_resume = gt9764_runtime_resume;
	runtime_suspend = gt9764_runtime_suspend;
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
	assert(gt9764_close(&lens->sd, NULL) == -EAGAIN);
	assert(lens->power_error == -EIO && !pm_status_error);
	assert(lens->supplies_enabled == BIT(1) && rails[1].disable_failures == 1);
	struct regulator peer = { .rdev = &rails[1] };
	assert(regulator_enable(&supplies[1]) == -ENOTRECOVERABLE);
	assert(!regulator_enable(&peer) && peer.enable_count == 1);
	assert(!regulator_disable_with_status(&peer, &(bool){false}));
	rails[1].status = 0;
	assert(regulator_check_disable_fault(&peer) == -EAGAIN);
	rails[1].status = -EIO;
	assert(regulator_check_disable_fault(&peer) == -EIO);
	disable_failure = NULL;
	assert(gt9764_open(&lens->sd, NULL) == -EIO && !usage);
	assert(!lens->supplies_enabled && !rails[1].disable_failures && !lens->power_error);
	assert(!gt9764_open(&lens->sd, NULL) && !gt9764_close(&lens->sd, NULL));
	gt9764_put(lens);
	puts("PASS retained lens disable permits a confirmed-on peer and recovers without detach");

	lens = fresh_lens();
	assert(!gt9764_open(&lens->sd, NULL));
	retained_failure = &supplies[1];
	assert(gt9764_close(&lens->sd, NULL) == -EAGAIN);
	assert(lens->power_off_work.pending);
	retained_failure = NULL;
	run_power_off_work(&lens->power_off_work);
	assert(!lens->power_off_work.pending && !lens->supplies_enabled && !lens->power_error && !active);
	assert(!gt9764_open(&lens->sd, NULL));
	lens->power_off_work.work.run(&lens->power_off_work.work);
	assert(active && usage == 1 && lens->supplies_enabled == (BIT(0) | BIT(1)));
	assert(!gt9764_close(&lens->sd, NULL));
	gt9764_put(lens);
	puts("PASS idle work clears a retained lens vote and cannot suspend an open lens");

	lens = fresh_lens();
	assert(!gt9764_open(&lens->sd, NULL));
	disable_failure = &supplies[1];
	assert(gt9764_close(&lens->sd, NULL) == -EAGAIN);
	disable_failure = NULL;
	assert(!gt9764_suspend(&dev) && disabled && !active && !lens->power_error);
	assert(!lens->power_off_work.pending);
	assert(!pm_runtime_force_resume(&dev));
	gt9764_put(lens);
	puts("PASS system suspend retries a retained lens vote before entering sleep");

	lens = fresh_lens();
	assert(!gt9764_open(&lens->sd, NULL));
	disable_failure = &supplies[1];
	assert(gt9764_close(&lens->sd, NULL) == -EAGAIN);
	disable_failure = NULL;
	gt9764_stop_pm(lens);
	assert(!lens->supplies_enabled && !supplies[1].enable_count && !rails[1].disable_failures);
	assert(lens->power_off_work.disabled && !lens->power_off_work.pending);
	gt9764_put(lens);
	puts("PASS detach retries a disable that retained the consumer reference");

	lens = fresh_lens();
	assert(!gt9764_open(&lens->sd, NULL));
	disable_failure = &supplies[1];
	fail_after_release = true;
	assert(gt9764_close(&lens->sd, NULL) == -EIO);
	assert(!lens->supplies_enabled && !supplies[1].enable_count && !usage);
	assert(gt9764_open(&lens->sd, NULL) == -EINVAL);
	assert(!usage && !lens->open_count && rails[1].disable_failures == 1);
	peer.rdev = &rails[1];
	assert(regulator_check_disable_fault(&peer) == -ENOTRECOVERABLE);
	gt9764_stop_pm(lens);
	gt9764_put(lens);
	puts("PASS post-release fault rejects an active PM get without a double disable");

	lens = fresh_lens();
	assert(!gt9764_open(&lens->sd, NULL));
	disable_failure = &supplies[1];
	fail_after_release = true;
	retained_failure = &supplies[0];
	assert(gt9764_close(&lens->sd, NULL) == -EAGAIN);
	assert(lens->supply_error && lens->supplies_enabled == BIT(0) && !pm_status_error);
	retained_failure = NULL;
	run_power_off_work(&lens->power_off_work);
	assert(!lens->supplies_enabled && !lens->power_off_work.pending && pm_status_error == -EIO);
	assert(gt9764_open(&lens->sd, NULL) == -EINVAL);
	gt9764_stop_pm(lens);
	gt9764_put(lens);
	puts("PASS a fatal lens error still permits cleanup of another retained vote");

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
	INIT_DELAYED_WORK(&sensor->power_off_work, SENSOR_POWER_OFF_WORK);
	fresh(&sensor->sd, sensor->supplies, ARRAY_SIZE(sensor->supplies));
	runtime_resume = SENSOR_RESUME;
	runtime_suspend = SENSOR_SUSPEND;
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
	assert(pm_runtime_put_sync(&dev) == -EAGAIN && active && !usage);
	assert(sensor.power_error == -EIO && !pm_status_error);
	disable_failure = NULL;
	assert(SENSOR_STREAM(&sensor.sd, 1) == -EIO && !usage && !cci_calls);
	assert(!sensor.supplies_enabled && !sensor.power_error && !active);
	assert(!SENSOR_STREAM(&sensor.sd, 1) && !SENSOR_STREAM(&sensor.sd, 0));
	puts("PASS a retained disable retries without unsafe sensor I/O or a latched PM error");

	fresh_sensor(&sensor);
	assert(!pm_runtime_resume_and_get(&dev));
	disable_failure = &supplies[2];
	fail_after_release = true;
	assert(pm_runtime_put_sync(&dev) == -EIO && active && !usage);
	assert(SENSOR_STREAM(&sensor.sd, 1) == -EINVAL && !usage && !cci_calls);
	assert(!sensor.streaming && !sensor.supplies_enabled);
	puts("PASS a post-release fault blocks reuse without a double disable");

	fresh_sensor(&sensor);
	assert(!pm_runtime_resume_and_get(&dev));
	retained_failure = &supplies[2];
	assert(pm_runtime_put_sync(&dev) == -EAGAIN && active && !usage);
	for (unsigned int attempt = 0; attempt < 3; attempt++)
		run_power_off_work(&sensor.power_off_work);
	assert(!sensor.power_off_work.pending && sensor.supplies_enabled == BIT(2));
	assert(!pm_status_error && !cci_calls);
	retained_failure = NULL;
	assert(!pm_runtime_suspend(&dev) && !sensor.supplies_enabled);
	puts("PASS persistent runtime suspend faults have a bounded retry budget");

	fresh_sensor(&sensor);
	assert(!pm_runtime_resume_and_get(&dev));
	disable_failure = &supplies[2];
	fail_after_release = true;
	retained_failure = &supplies[1];
	assert(pm_runtime_put_sync(&dev) == -EAGAIN && !pm_status_error);
	assert(sensor.supplies_enabled == BIT(1) && sensor.supply_error);
	retained_failure = NULL;
	run_power_off_work(&sensor.power_off_work);
	assert(!sensor.supplies_enabled && pm_status_error == -EIO);
	assert(SENSOR_STREAM(&sensor.sd, 1) == -EINVAL && !cci_calls);
	puts("PASS a fatal sensor error still permits cleanup of another retained vote");

	fresh_sensor(&sensor);
	enable_failure = &supplies[1];
	disable_failure = &supplies[0];
	assert(SENSOR_STREAM(&sensor.sd, 1) == -EAGAIN && !usage && !active);
	assert(sensor.supplies_enabled == BIT(0) && !pm_status_error);
	enable_failure = disable_failure = NULL;
	assert(!SENSOR_STREAM(&sensor.sd, 1) && !SENSOR_STREAM(&sensor.sd, 0));
	assert(!sensor.supplies_enabled && !usage);
	puts("PASS partial resume rollback retries the retained disable before any new enable");

	fresh_sensor(&sensor);
	enable_failure = &supplies[1];
	retained_failure = &supplies[0];
	assert(SENSOR_STREAM(&sensor.sd, 1) == -EAGAIN && !usage && !active);
	assert(SENSOR_SYSTEM_SUSPEND(&dev) == -EIO && !disabled);
	retained_failure = NULL;
	assert(!SENSOR_SYSTEM_SUSPEND(&dev) && disabled && !sensor.supplies_enabled);
	assert(!pm_runtime_force_resume(&dev));
	puts("PASS system suspend checks residual votes even from a failed runtime resume");
	return 0;
}
#endif
