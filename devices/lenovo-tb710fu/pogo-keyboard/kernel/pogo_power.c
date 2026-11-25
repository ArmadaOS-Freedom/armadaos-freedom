// SPDX-License-Identifier: GPL-2.0
/*
 * tb710fu pogo keyboard 3.3V rail control.
 *
 * The pogo magnetic keyboard needs 3.3V on vreg_l8b_1p8, but raising that
 * regulator during boot kills the panel (verified on SteamOS and ArmadaOS),
 * so it must be done at runtime: echo 1 > /sys/class/pogo/power/enable
 */

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/platform_device.h>
#include <linux/of.h>
#include <linux/regulator/consumer.h>
#include <linux/device.h>
#include <linux/sysfs.h>
#include <linux/slab.h>
#include <linux/gpio/consumer.h>
#include <linux/gpio/machine.h>
#include <linux/delay.h>

#define DRIVER_NAME "pogo_power"

struct pogo_power {
	struct device *dev;
	struct regulator *vdd33;
	struct regulator *vdd18;
	bool vdd33_shared;
	bool vdd18_shared;
	struct class *cls;
	struct device *pdev;
	bool enabled;
	struct gpio_desc *txen;
};

static struct pogo_power *g_pogo;

/* tx-en: half-duplex direction control for the pogo UART.
 * low = receive (keyboard -> host), high = transmit (host -> keyboard).
 * Android drives it low; without it the keyboard never sends a byte. */
static int txen_gpio = 100;
module_param(txen_gpio, int, 0644);
MODULE_PARM_DESC(txen_gpio, "GPIO number for pogo UART tx-en (default 100, -1 disables)");

/* Target voltage for the keyboard 3.3V rail. -1 = use the built-in preference
 * list (starts at 3.296V, drops to 2.96V and below). Set e.g.
 *   insmod pogo_power.ko vdd33_uv=2960000
 * to pin it, which is how we A/B stability without touching the boot image.
 */
static int vdd33_uv = -1;
module_param(vdd33_uv, int, 0644);
MODULE_PARM_DESC(vdd33_uv, "pogo 3.3V rail target in uV (-1 = auto)");

static struct pogo_power *g_pogo;

static struct gpiod_lookup_table pogo_txen_lookup = {
	.dev_id = "pogo-power",
	.table = {
		GPIO_LOOKUP("f100000.pinctrl", 100, "tx-en", GPIO_ACTIVE_HIGH),
		{ }
	},
};

/* The live DT (as packed by the author) has no vdd-1p8 supply for this node,
 * so also try the regulator's global name. */
static struct regulator *pogo_get_reg(struct device *dev, const char *supply,
				      const char *name, bool *shared)
{
	struct regulator *r;

	r = devm_regulator_get_optional(dev, supply);
	if (!IS_ERR(r)) {
		if (shared)
			*shared = false;
		return r;
	}

	r = regulator_get(NULL, name);
	if (IS_ERR(r))
		return NULL;

	dev_info(dev, "%s resolved by global name\n", name);
	if (shared)
		*shared = true;
	return r;
}


static ssize_t enable_show(struct device *dev, struct device_attribute *attr,
			   char *buf)
{
	return sysfs_emit(buf, "%d\n", g_pogo && g_pogo->enabled);
}

static ssize_t enable_store(struct device *dev, struct device_attribute *attr,
			    const char *buf, size_t count)
{
	struct pogo_power *p = g_pogo;
	bool on;
	int ret;

	if (!p)
		return -ENODEV;
	ret = kstrtobool(buf, &on);
	if (ret)
		return ret;

	if (on == p->enabled)
		return count;

	if (on) {
		/* vreg_l8b_1p8 on this PMIC tops out at 3296000 uV (it is reported as
		 * "unsupportable voltage range: 3300000-3296000uV"), so try that
		 * first: running the keyboard at 2.96V looks like it browns out.
		 */
		/* Boot default stays at the proven 2.96V: 3.3V on this rail also
		 * feeds other board IO and has black-screened the panel before.
		 * Use insmod pogo_power.ko vdd33_uv=3296000 to test higher values.
		 */
		static const int mv[] = { 2960000, 2850000, 2700000, 2500000 };
		int i, chosen = 0;

		if (p->vdd18) {
			regulator_set_voltage(p->vdd18, 1800000, 1800000);
		}
		if (vdd33_uv > 0) {
			ret = regulator_set_voltage(p->vdd33, vdd33_uv, vdd33_uv);
			if (!ret) {
				chosen = vdd33_uv;
				dev_info(p->dev, "rail pinned to %d uV by module param\n", chosen);
			} else {
				dev_warn(p->dev, "pinned %d uV rejected: %d, falling back\n",
					 vdd33_uv, ret);
			}
		}
		if (p->vdd18 && regulator_enable(p->vdd18))
			dev_warn(p->dev, "1.8V enable failed\n");
		else if (p->vdd18)
			dev_info(p->dev, "1.8V rail on\n");

		for (i = 0; !chosen && i < ARRAY_SIZE(mv); i++) {
			ret = regulator_set_voltage(p->vdd33, mv[i], mv[i]);
			if (!ret) {
				chosen = mv[i];
				break;
			}
			dev_warn(p->dev, "set_voltage(%d uV) failed: %d\n", mv[i], ret);
		}
		if (!chosen)
			dev_warn(p->dev, "no usable voltage, using current\n");
		else
			dev_info(p->dev, "rail set to %d uV\n", chosen);
		ret = regulator_enable(p->vdd33);
		if (ret) {
			dev_err(p->dev, "enable failed: %d\n", ret);
			return ret;
		}
		p->enabled = true;
		dev_info(p->dev, "pogo 3.3V on\n");
	} else {
		regulator_disable(p->vdd33);
		if (p->vdd18)
			regulator_disable(p->vdd18);
		p->enabled = false;
		dev_info(p->dev, "pogo 3.3V off\n");
	}
	return count;
}

static DEVICE_ATTR_RW(enable);

static ssize_t txen_show(struct device *dev, struct device_attribute *attr,
			 char *buf)
{
	if (!g_pogo || !g_pogo->txen)
		return -ENODEV;
	return sysfs_emit(buf, "%d\n", gpiod_get_value_cansleep(g_pogo->txen));
}

static ssize_t txen_store(struct device *dev, struct device_attribute *attr,
			  const char *buf, size_t count)
{
	bool on;
	int ret;

	if (!g_pogo || !g_pogo->txen)
		return -ENODEV;
	ret = kstrtobool(buf, &on);
	if (ret)
		return ret;
	if (on) {
		/* let the last inbound burst finish, then turn the line around */
		usleep_range(2000, 4000);
		gpiod_set_value_cansleep(g_pogo->txen, 1);
		udelay(200);
	} else {
		udelay(200);
		gpiod_set_value_cansleep(g_pogo->txen, 0);
	}
	return count;
}

static DEVICE_ATTR_RW(txen);

static struct attribute *pogo_attrs[] = {
	&dev_attr_enable.attr,
	&dev_attr_txen.attr,
	NULL,
};

static const struct attribute_group pogo_attr_group = {
	.attrs = pogo_attrs,
};

static int pogo_probe(struct platform_device *pdev)
{
	struct pogo_power *p;
	int ret;

	p = devm_kzalloc(&pdev->dev, sizeof(*p), GFP_KERNEL);
	if (!p)
		return -ENOMEM;

	p->dev = &pdev->dev;
	p->vdd18 = pogo_get_reg(p->dev, "vdd-1p8", "vreg_l1b_1p8", &p->vdd18_shared);
	if (!p->vdd18)
		dev_warn(p->dev, "no 1.8V rail found\n");

	p->vdd33 = pogo_get_reg(p->dev, "vdd-3p3", "vreg_l8b_1p8", &p->vdd33_shared);
	if (!p->vdd33) {
		dev_err(p->dev, "no 3.3V rail (vreg_l8b_1p8) found\n");
		return -ENODEV;
	}

	p->txen = devm_gpiod_get(p->dev, "tx-en", GPIOD_OUT_LOW);
	if (IS_ERR(p->txen)) {
		dev_warn(p->dev, "tx-en gpio unavailable: %ld\n",
			 PTR_ERR(p->txen));
		p->txen = NULL;
	} else {
		gpiod_set_value_cansleep(p->txen, 0);
		dev_info(p->dev, "tx-en driven low\n");
	}

	p->cls = class_create(DRIVER_NAME);
	if (IS_ERR(p->cls))
		return PTR_ERR(p->cls);

	p->pdev = device_create(p->cls, p->dev, MKDEV(0, 0), NULL, "power");
	if (IS_ERR(p->pdev)) {
		ret = PTR_ERR(p->pdev);
		class_destroy(p->cls);
		return ret;
	}

	ret = sysfs_create_group(&p->pdev->kobj, &pogo_attr_group);
	if (ret) {
		device_destroy(p->cls, MKDEV(0, 0));
		class_destroy(p->cls);
		return ret;
	}

	g_pogo = p;
	platform_set_drvdata(pdev, p);
	dev_info(p->dev, "ready: echo 1 > /sys/class/pogo_power/power/enable (tx-en gpio %d)\n", txen_gpio);
	return 0;
}

static void pogo_remove(struct platform_device *pdev)
{
	struct pogo_power *p = platform_get_drvdata(pdev);

	if (!p)
		return;
	if (p->enabled) {
		regulator_disable(p->vdd33);
		p->enabled = false;
	}
	if (p->vdd33_shared)
		regulator_put(p->vdd33);
	if (p->vdd18_shared && p->vdd18)
		regulator_put(p->vdd18);
	sysfs_remove_group(&p->pdev->kobj, &pogo_attr_group);
	device_destroy(p->cls, MKDEV(0, 0));
	class_destroy(p->cls);
	g_pogo = NULL;
}

static const struct of_device_id pogo_of_match[] = {
	{ .compatible = "tb710fu,pogo-power" },
	{ }
};
MODULE_DEVICE_TABLE(of, pogo_of_match);

static struct platform_driver pogo_driver = {
	.probe = pogo_probe,
	.remove = pogo_remove,
	.driver = {
		.name = DRIVER_NAME,
		.of_match_table = pogo_of_match,
	},
};

static int __init pogo_init(void)
{
	pogo_txen_lookup.table[0].chip_hwnum = txen_gpio;
	if (txen_gpio >= 0)
		gpiod_add_lookup_table(&pogo_txen_lookup);
	return platform_driver_register(&pogo_driver);
}

static void __exit pogo_exit(void)
{
	platform_driver_unregister(&pogo_driver);
	if (txen_gpio >= 0)
		gpiod_remove_lookup_table(&pogo_txen_lookup);
}

module_init(pogo_init);
module_exit(pogo_exit);

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("TB710FU pogo keyboard 3.3V rail control");
