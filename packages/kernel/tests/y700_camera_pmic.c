#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>

#define BIT(index) (1U << (index))
#define GENMASK(high, low) ((BIT((high) + 1) - 1) & ~(BIT(low) - 1))
#define EPROBE_DEFER 517

typedef unsigned char u8;

struct device { bool vsys, vin7; };
struct gpio_desc;
struct mutex { bool locked; };
struct regmap { unsigned int value; };
struct regulator_desc { unsigned int enable_mask; };
struct regulator_dev { const struct regulator_desc *desc; void *data; };

static unsigned int writes, fail_write, accept_timeout, reads;
static bool bus_down;
static const char *control_supply;
static int supply_result;

static void unlock_guard(struct mutex **lock)
{
	assert((*lock)->locked);
	(*lock)->locked = false;
}

static struct mutex *lock_mutex(struct mutex *lock)
{
	assert(!lock->locked);
	lock->locked = true;
	return lock;
}

#define guard(kind) __attribute__((cleanup(unlock_guard))) struct mutex *lock = lock_mutex

static void *rdev_get_drvdata(struct regulator_dev *rdev) { return rdev->data; }
static void dev_err(struct device *dev, const char *format, ...) {}
static int dev_err_probe(struct device *dev, int ret, const char *format, ...) { return ret; }

static int regmap_read(struct regmap *regmap, unsigned int reg, unsigned int *value)
{
	assert(reg == WL2868C_LDO_ENABLE);
	reads++;
	if (bus_down)
		return -EIO;
	*value = regmap->value;
	return 0;
}

static int regmap_write(struct regmap *regmap, unsigned int reg, unsigned int value)
{
	assert(reg == WL2868C_LDO_ENABLE);
	writes++;
	if (writes == accept_timeout) {
		regmap->value = value;
		return -ETIMEDOUT;
	}
	if (bus_down || writes == fail_write)
		return -EIO;
	regmap->value = value;
	return 0;
}

static int regmap_bulk_write(struct regmap *regmap, unsigned int reg, const u8 *values,
			     size_t size)
{
	assert(reg == WL2868C_LDO_SEQ && size == WL2868C_LDO_SEQ_LEN);
	while (size)
		assert(!values[--size]);
	return 0;
}

static bool device_property_present(struct device *dev, const char *name)
{
	return strcmp(name, "vsys-supply") == 0 ? dev->vsys : dev->vin7;
}

static int devm_regulator_get_enable_optional(struct device *dev, const char *name)
{
	control_supply = name;
	return supply_result;
}

/* FUNCTIONS */

int main(void)
{
	const struct regulator_desc desc1 = { BIT(0) }, desc7 = { BIT(6) };
	struct device dev = {0};
	struct regmap regmap = { .value = BIT(6) };
	struct wl2868c chip = { .dev = &dev, .regmap = &regmap };
	struct regulator_dev ldo1 = { &desc1, &chip }, ldo7 = { &desc7, &chip };
	const unsigned int both = WL2868C_SYS_ENABLE | BIT(0) | BIT(6);

	assert(!wl2868c_enable_control_supply(&dev) && !control_supply);
	dev.vin7 = true;
	supply_result = -EPROBE_DEFER;
	assert(wl2868c_enable_control_supply(&dev) == -EPROBE_DEFER);
	assert(!strcmp(control_supply, "vin7"));
	dev.vsys = true;
	supply_result = 0;
	assert(!wl2868c_enable_control_supply(&dev) && !strcmp(control_supply, "vsys"));
	puts("PASS control supply is optional, prefers VSYS and propagates VIN7 deferral");

	assert(!wl2868c_init_outputs(&chip) && !chip.enabled);
	assert(!wl2868c_enable(&ldo1) && regmap.value == (WL2868C_SYS_ENABLE | BIT(0)));
	unsigned int reads_before = reads;
	accept_timeout = writes + 1;
	assert(!wl2868c_enable(&ldo7) && regmap.value == both);
	assert(writes == accept_timeout && reads == reads_before + 1);
	assert(chip.enabled == (BIT(0) | BIT(6)) && wl2868c_is_enabled(&ldo7) == 1);
	puts("PASS accepted timeout is reconciled without adopting firmware presets");

	fail_write = writes + 1;
	assert(!wl2868c_disable(&ldo1) && writes == fail_write + 1);
	assert(regmap.value == (WL2868C_SYS_ENABLE | BIT(6)) && chip.enabled == BIT(6));
	assert(wl2868c_is_enabled(&ldo7) == 1);
	assert(!wl2868c_disable(&ldo7) && !regmap.value && !chip.enabled);
	puts("PASS transient disable error retries without dropping another output");

	accept_timeout = writes + 1;
	bus_down = true;
	assert(wl2868c_enable(&ldo7) == -ENOTRECOVERABLE);
	assert(!chip.enabled && (regmap.value & BIT(6)));
	bus_down = false;
	assert(!wl2868c_is_enabled(&ldo7));
	assert(!wl2868c_enable(&ldo7) && wl2868c_is_enabled(&ldo7) == 1);
	assert(!wl2868c_enable(&ldo1) && regmap.value == both);
	assert(!chip.lock.locked);
	puts("PASS untracked physical output cannot be adopted without a tracked enable");
	return 0;
}
