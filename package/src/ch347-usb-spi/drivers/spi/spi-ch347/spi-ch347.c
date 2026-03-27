/*
 * USB to SPI/I2C/GPIO master driver for USB converter chip ch341/ch347, etc.
 *
 * Copyright (C) 2023 Nanjing Qinheng Microelectronics Co., Ltd.
 * Web: http://wch.cn
 * Author: WCH <tech@wch.cn>
 *
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 2 of the License, or
 * (at your option) any later version.
 *
 * Update Log:
 * v1.0 - initial version (2023.03)
 *
 * This code is modified to support Newracom NRC7394 device. (www.newracom.com)
 *
 */

#ifndef CONFIG_CH347_GPIO
//#define CONFIG_CH347_GPIO

#ifndef CONFIG_CH347_GPIO_IRQ
//#define CONFIG_CH347_GPIO_IRQ
#endif
#endif /* #ifndef CONFIG_CH347_GPIO */

#include <linux/version.h>
#if LINUX_VERSION_CODE < KERNEL_VERSION(3, 4, 0)
#error The driver requires at least kernel version 3.4
#endif

#include <linux/kernel.h>
#include <linux/errno.h>
#include <linux/module.h>
#include <linux/types.h>
#include <linux/slab.h>
#include <linux/usb.h>
#include <linux/spi/spi.h>
#include <linux/irq.h>
#include <linux/platform_device.h>
#include <linux/gpio.h>
#if LINUX_VERSION_CODE >= KERNEL_VERSION(3, 17, 0)
#include <linux/gpio/machine.h>
#endif
#if LINUX_VERSION_CODE >= KERNEL_VERSION(3, 13, 0)
#include <linux/gpio/consumer.h>
#include <linux/gpio/driver.h>
#endif

#include <linux/idr.h>
#include <linux/usb.h>

/*************************************************************************************************/

#define DEV_TRACE(d) 			dev_dbg(d, "%s,%d\n", __FUNCTION__, __LINE__)

#define DEV_DBG(d, f, ...)  	dev_dbg(d, "%s: " f "\n", __FUNCTION__, ##__VA_ARGS__)
#define DEV_ERR(d, f, ...)  	dev_err(d, "%s: " f "\n", __FUNCTION__, ##__VA_ARGS__)
#define DEV_INFO(d, f, ...) 	dev_info(d, "%s: " f "\n", __FUNCTION__, ##__VA_ARGS__)

#if defined(DEBUG)
#define HEX_DUMP(prefix_str, buf, len, ascii)	\
		print_hex_dump(KERN_DEBUG, prefix_str, DUMP_PREFIX_OFFSET, 16, 1, buf, len, ascii)
#else
#define HEX_DUMP(prefix_str, buf, len, ascii)
#endif

/*************************************************************************************************/

#ifndef USB_DEVICE_INTERFACE_NUMBER
#define USB_DEVICE_INTERFACE_NUMBER(vend, prod, num) \
	.match_flags = USB_DEVICE_ID_MATCH_DEVICE | USB_DEVICE_ID_MATCH_INT_NUMBER, .idVendor = (vend), \
	.idProduct = (prod), .bInterfaceNumber = (num)
#endif

#ifndef irq_data_to_desc
#define irq_data_to_desc(data) 	container_of(data, struct irq_desc, irq_data)
#endif

#define CHECK_PARAM(cond) \
	if (!(cond)) \
		return;

#define CHECK_PARAM_RET(cond, err) \
	if (!(cond)) \
		return err;

#define CH347_USBDEV             	(&(ch347_dev->intf->dev))

#define CH347_IO_LOCK()            	mutex_lock(&ch347_dev->io_mutex)
#define CH347_IO_UNLOCK()          	mutex_unlock(&ch347_dev->io_mutex)

#define CH347_OPS_LOCK()           	mutex_lock(&ch347_dev->ops_mutex)
#define CH347_OPS_UNLOCK()         	mutex_unlock(&ch347_dev->ops_mutex)

/*************************************************************************************************/

#define CH347_GPIO_NUM					8

#define CH347_SPI_CS_NUM 				2
#define CH347_SPI_FREQ_MIN				(500 * 1000)   		/* HZ */
#define CH347_SPI_FREQ_MAX				(60 * 1000 * 1000) 	/* HZ */

#define CH347_USB_HDR_LEN   			3

#define CH347_USB_CMD_NONE				0xFF
#define CH347_USB_CMD_SPI_INIT 			0xC0 /* SPI Init Command                                                 */
#define CH347_USB_CMD_SPI_CONTROL 		0xC1 /* SPI Control Command,                                             */
											 /* used to control the SPI interface chip selection pin output high */
											 /* or low level and delay time                                      */
#define CH347_USB_CMD_SPI_RD_WR			0xC2 /* SPI general read and write command,                              */
											 /*	used for SPI general read and write operation,                   */
											 /*	and for short packet communication                               */
#define CH347_USB_CMD_SPI_BLCK_RD		0xC3 /* SPI read data command in batch,                                  */
											 /*	is generally used for the batch data read operation.             */
#define CH347_USB_CMD_SPI_BLCK_WR 		0xC4 /* SPI write data command in batch,                                 */
											 /*	is generally used for the batch data write operation.            */
#define CH347_USB_CMD_INFO_RD 			0xCA /* Parameter acquisition command,                                   */
											 /*	used to obtain SPI interface related parameters, etc             */
#define CH347_USB_CMD_GPIO_CMD 			0xCC /* GPIO Command                                                     */

//#define CH347_USB_BULK_LEN_MAX		510

#define CH347_USB_MAX_BUFFER_SIZE		(4 * 1024)

#define CH347_USB_RW_TIMEOUT			5000 /* msec */

/*************************************************************************************************/

enum CHIP_TYPE {
	CHIP_CH347T = 0,
	CHIP_CH347F,

	CHIP_TYPE_MAX
};

#pragma pack(1)

/* SPI setting structure */
typedef struct {
	u8 imode;  						/* 0-3: SPI Mode0/1/2/3                                                  */
	u8 iclock; 						/* 0: 60MHz, 1: 30MHz, 2: 15MHz, 3: 7.5MHz,                              */
									/* 4: 3.75MHz, 5: 1.875MHz, 6: 937.5KHz，7: 468.75KHz                    */
	u8 ibyteorder;	      			/* 0: LSB, 1: MSB                                                        */
	u16 ispi_rw_interval; 			/* SPI read and write interval, unit: us                                 */
	u8 ispi_out_def;      			/* SPI output data by default while read                                 */
	u8 ics[CH347_SPI_CS_NUM];	  	/* SPI chip select, valid while BIT7 is 1, low byte: CS0, high byte: CS1 */
	u8 cs_polar[CH347_SPI_CS_NUM]; 	/* BIT0：CS polar control, 0：low active, 1：high active                 */
	u8 iauto_de_cs;      			/* automatically undo the CS after operation completed                   */
	u16 iactive_delay;    			/* delay time of read and write operation after setting CS, unit: us     */
	u16 ideactive_delay;  			/* delay time of read and write operation after canceling CS, unit: us   */
} mspi_cfgs;

/* SPI Init structure definition */
typedef struct {
	u16 spi_direction;		/* Specifies the SPI unidirectional or bidirectional data mode.        */
                            /* This parameter can be a value of @ref SPI_data_direction            */
	u16 spi_mode;			/* Specifies the SPI operating mode.                                   */
                            /* This parameter can be a value of @ref SPI_mode                      */
	u16 spi_datasize;		/* Specifies the SPI data size.                                        */
                            /* This parameter can be a value of @ref SPI_data_size                 */
	u16 s_spi_cpol;			/* Specifies the serial clock steady state.                            */
                            /* This parameter can be a value of @ref SPI_Clock_Polarity            */
	u16 s_spi_cpha;			/* Specifies the clock active edge for the bit capture.                */
                            /* This parameter can be a value of @ref SPI_Clock_Phase               */
	u16 spi_nss;			/* Specifies whether the NSS signal is managed by hardware (NSS pin)   */
							/* or by software using the SSI bit.                                   */
                            /* This parameter can be a value of @ref SPI_Slave_Select_management   */
	u16 spi_baudrate_scale; /* Specifies the Baud Rate prescaler value                             */
							/* which will be used to configure the transmit and receive SCK clock. */
                            /* This parameter can be a value of @ref SPI_BaudRate_Prescaler.       */
                            /* @note The communication clock is derived from the master clock.     */
							/*       The slave clock does not need to be set.                      */
	u16 spi_firstbit;		/* Specifies whether data transfers start from MSB or LSB bit.         */
                            /* This parameter can be a value of @ref SPI_MSB_LSB_transmission      */
	u16 spi_crc_poly;		/* Specifies the polynomial used for the CRC calculation.              */
} spi_init_typedef;

typedef struct {
	spi_init_typedef spi_initcfg;

	u16 spi_rw_interval; 	/* SPI read and write interval, unit: us                                 */
	u8 spi_outdef;	     	/* SPI output data by default while read                                 */
	u8 misc_cfg;	     	/* misc option                                                           */
                            /* BIT7: CS0 polar control, 0：low active, 1：high active                */
                            /* BIT6：CS2 polar control, 0：low active, 1：high active                */
                            /* BIT5：I2C clock stretch control, 0：disable 1: enable                 */
                            /* BIT4：generates NACK or not when read the last byte for I2C operation */
                            /* BIT3-0：reserved                                                      */

	u8 reserved[4];	     	/* reserved */
} stream_hw_cfgs;

#pragma pack()

struct ch347_gpio {
	int id;
	int pin;
	bool irq;
	bool output;
};

/* device specific structure */
struct ch347_device {
	enum CHIP_TYPE chiptype;

	struct mutex io_mutex;
	struct mutex ops_mutex;

	struct usb_device *usb_dev; /* usb device */
	struct usb_interface *intf; /* usb interface */

	struct usb_endpoint_descriptor *bulk_in;  /* usb endpoint bulk in */
	struct usb_endpoint_descriptor *bulk_out; /* usb endpoint bulk out */
	struct usb_endpoint_descriptor *intr_in;  /* usb endpoint interrupt in */
	
	u8 bulkin_buf[CH347_USB_MAX_BUFFER_SIZE * 2];  /* usb bulk in buffer */
	u8 bulkout_buf[CH347_USB_MAX_BUFFER_SIZE * 2]; /* usb bulk out buffer */
	
	struct urb *intr_urb;

	int id;
	struct platform_device *spi_pdev;
	struct spi_master *spi_master;
	
	stream_hw_cfgs hwcfg;
	mspi_cfgs spicfg;

#if defined(CONFIG_CH347_GPIO)
	int gpio_num;
	struct ch347_gpio *gpio_config;
	struct gpio_chip gpio_chip;
#endif

	/* irq device description */
#if defined(CONFIG_CH347_GPIO_IRQ)
	struct irq_chip irq_chip; /* chip descriptor for IRQs */
	spinlock_t irq_lock;

	u8 irq_num; /* number of pins with IRQs */
	u8 irq_mask; /* IRQ enabled flag (irq_num elements) */
	u32 irq_type; /* IRQ types (irq_num elements) */
	int irq_base; /* base IRQ allocated */

	u8 intrin_buf[CH347_USB_MAX_BUFFER_SIZE]; /* usb interrupt in buffer */
#endif	
};

/*************************************************************************************************/

static int param_usb_timeout = CH347_USB_RW_TIMEOUT;
module_param_named(usb_timeout, param_usb_timeout, int, 0600);
MODULE_PARM_DESC(usb_timeout, "USB Read/Write Timeout (if 0, the wait is forever)(default: 5000 ms");

static int param_bus_num = -1;
module_param_named(spi_bus_num, param_bus_num, int, 0600);
MODULE_PARM_DESC(spi_bus_num, "SPI master bus number (if negative, dynamic allocation)(default: -1)");

#if defined(CONFIG_CH347_GPIO)
static int param_gpio_base = -1;
module_param_named(gpio_base, param_gpio_base, int, 0600);
MODULE_PARM_DESC(gpio_base, "first GPIO number handled by GPIO controller driver (if negative, dynamic allocation)(default: -1)");
#endif

/*************************************************************************************************/

static DEFINE_IDA(ch347_devid_ida);

static const struct usb_device_id ch347_usb_ids[] = {
	{ USB_DEVICE_INTERFACE_NUMBER(0x1a86, 0x55db, 0x02) }, /* CH347T Mode1 */
	{ USB_DEVICE_INTERFACE_NUMBER(0x1a86, 0x55de, 0x04) }, /* CH347F       */
	{} /* Terminating entry */
};
MODULE_DEVICE_TABLE(usb, ch347_usb_ids);

#if defined(CONFIG_CH347_GPIO)
struct ch347_gpio ch347f_gpio_config[CH347_GPIO_NUM + 1] = {
	{  0, 17, false, true }, /* CTS0/GPIO0       */
	{  1, 18, false, true }, /* RTS0/GPIO1       */
	{  2, 10, false, true }, /* DTR0/TNOW0/GPIO2 */
	{  3,  9, false, true }, /* TRST/GPIO3       */
	{  4, 23, false, true }, /* TCK/SWDCLK/GPIO4 */
	{  5, 24, false, true }, /* TDO/GPIO5        */
	{  6, 25, false, true }, /* TDI/GPIO6        */
	{  7, 26, false, true }, /* TMS/SWDIO/GPIO7  */
	{ -1, }
};
#endif

/*************************************************************************************************/

#if !defined(DEBUG)
static int ch347_usb_read (struct ch347_device *ch347_dev, u8 cmd, u16 len, void *data);
static int ch347_usb_write (struct ch347_device *ch347_dev, u8 cmd, u16 len, void *data);
#else
static int _ch347_usb_read (struct ch347_device *ch347_dev, u8 cmd, u16 len, void *data, const char *caller);
static int _ch347_usb_write (struct ch347_device *ch347_dev, u8 cmd, u16 len, void *data, const char *caller);
#define  ch347_usb_read(dev, cmd, len, data)	_ch347_usb_read(dev, cmd, len, data, __func__)
#define  ch347_usb_write(dev, cmd, len, data)	_ch347_usb_write(dev, cmd, len, data, __func__)
#endif

/*************************************************************************************************/

#if defined(CONFIG_CH347_GPIO_IRQ)

static void ch347_irq_enable_disable (struct irq_data *data, bool enable)
{
	struct ch347_device *ch347_dev = data ? irq_data_get_irq_chip_data(data) : NULL;
	int irq;

	CHECK_PARAM(ch347_dev);

	irq = data->irq - ch347_dev->irq_base;

	if (irq >= 0 && irq < ch347_dev->irq_num) {
		if (enable)
			ch347_dev->irq_mask |= (1 << irq);
		else
			ch347_dev->irq_mask &= ~(1 << irq);

		DEV_INFO(CH347_USBDEV, "IRQ %d %s", data->irq, enable ? "enable" : "disable");
	}
}

static void ch347_irq_enable (struct irq_data *data)
{
	ch347_irq_enable_disable(data, true);
}

static void ch347_irq_disable (struct irq_data *data)
{
	ch347_irq_enable_disable(data, false);
}

static int ch347_irq_set_type (struct irq_data *data, unsigned int type)
{
	struct ch347_device *ch347_dev = data ? irq_data_get_irq_chip_data(data) : NULL;
	int irq;

	CHECK_PARAM_RET(ch347_dev, -EINVAL);

	irq = data->irq - ch347_dev->irq_base;

	if (irq < 0 || irq >= ch347_dev->irq_num)
		return -EINVAL;

	ch347_dev->irq_type &= ~(0xf << (4 * irq));
	ch347_dev->irq_type |= ((type & 0xf) << (4 * irq));

	DEV_INFO(CH347_USBDEV, "IRQ %d flow_type=0x%X", data->irq, type);

	return 0;
}

#if 0
static int ch347_irq_check (struct ch347_device *ch347_dev, u8 irq, u8 old, u8 new, bool hardware)
{
	int type;

	CHECK_PARAM_RET(old != new, 0)
	CHECK_PARAM_RET(ch347_dev, -EINVAL);
	CHECK_PARAM_RET(irq < ch347_dev->irq_num, -EINVAL);

	// valid IRQ is in range 0 ... ch347_dev->irq_num-1, invalid IRQ is -1
	if (irq < 0 || irq >= ch347_dev->irq_num)
		return -EINVAL;

	// if IRQ is disabled, just return with success
	if (!ch347_dev->irq_enabled[irq])
		return 0;

	type = ch347_dev->irq_types[irq];

	// for software IRQs dont check if IRQ is the hardware IRQ for rising edges
	if (!hardware && irq == ch347_dev->irq_hw && new > old)
		return 0;

	if ((type & IRQ_TYPE_EDGE_FALLING && old > new) || (type & IRQ_TYPE_EDGE_RISING && new > old)) {
		unsigned long flags;
		// DEV_DBG (CH347_USBDEV, "%s irq=%d %d %s",
		//          hardware ? "hardware" : "software",
		//          irq, type, (old > new) ? "falling" : "rising");

		spin_lock_irqsave(&ch347_dev->irq_lock, flags);
#if LINUX_VERSION_CODE >= KERNEL_VERSION(4, 3, 0)
		handle_simple_irq(irq_data_to_desc(irq_get_irq_data(ch347_dev->irq_base + irq)));
#else
		handle_simple_irq(ch347_dev->irq_base + irq,
				  irq_data_to_desc(irq_get_irq_data(ch347_dev->irq_base + irq)));
#endif
		spin_unlock_irqrestore(&ch347_dev->irq_lock, flags);
	}

	return 0;
}
#endif

static void ch347_irq_handler (struct urb *urb)
{
	struct ch347_device *ch347_dev = urb ? urb->context : NULL;
	
	CHECK_PARAM(ch347_dev);
		
	DEV_INFO(CH347_USBDEV, "IRQ: status=%d length=%d", urb->status, urb->actual_length);

	if (urb->status != 0) {
		DEV_ERR(CH347_USBDEV, "urb_status=%d", urb->status);
		return;
	}
	
	if (urb->actual_length > 0) {
		print_hex_dump(KERN_INFO, "usb_irq: ", DUMP_PREFIX_OFFSET, 16, 1, urb->transfer_buffer, urb->actual_length, true);
	}

	usb_submit_urb(ch347_dev->intr_urb, GFP_ATOMIC);	
}

static int ch347_irq_probe (struct ch347_device *ch347_dev)
{
	int ret;
	int i;

	CHECK_PARAM_RET(ch347_dev, -EINVAL);

	if (ch347_dev->irq_num == 0) {
		DEV_INFO(CH347_USBDEV, "no IRQs");
		return 0;
	}

	ch347_dev->intr_urb = usb_alloc_urb(0, GFP_KERNEL);
	if (!ch347_dev->intr_urb) {
		DEV_ERR(CH347_USBDEV, "failed to alloc URB");
		return -ENOMEM;
	}

	ret= irq_alloc_descs(-1, 0, ch347_dev->irq_num, 0);
	if (ret < 0) {
		DEV_ERR(CH347_USBDEV, "failed to allocate IRQ descriptors (%d)", ret);
		return ret;
	}

	ch347_dev->irq_base = ret;
	ch347_dev->irq_mask = 0;
	ch347_dev->irq_type = 0;
	spin_lock_init(&ch347_dev->irq_lock);

	ch347_dev->irq_chip.name = "ch347";
	ch347_dev->irq_chip.irq_enable = ch347_irq_enable;
	ch347_dev->irq_chip.irq_disable = ch347_irq_disable;
	ch347_dev->irq_chip.irq_set_type = ch347_irq_set_type;

	for (i = 0; i < ch347_dev->irq_num; i++) {
		irq_set_chip(ch347_dev->irq_base + i, &ch347_dev->irq_chip);
		irq_set_chip_data(ch347_dev->irq_base + i, ch347_dev);
		irq_clear_status_flags(ch347_dev->irq_base + i, IRQ_NOREQUEST | IRQ_NOPROBE);
	}

	usb_fill_int_urb(ch347_dev->intr_urb, ch347_dev->usb_dev,
					usb_rcvintpipe(ch347_dev->usb_dev, usb_endpoint_num(ch347_dev->intr_in)), 
					ch347_dev->intrin_buf, sizeof(ch347_dev->intrin_buf), 
					ch347_irq_handler, ch347_dev,
			 		ch347_dev->intr_in->bInterval);

	usb_submit_urb(ch347_dev->intr_urb, GFP_ATOMIC); 

	DEV_INFO(CH347_USBDEV, "registered IRQ %d", ret);

	return 0;
}

static void ch347_irq_remove (struct ch347_device *ch347_dev)
{
	CHECK_PARAM(ch347_dev);

	if (ch347_dev->intr_urb)
	 	usb_free_urb(ch347_dev->intr_urb);

	if (ch347_dev->irq_base)
		irq_free_descs(ch347_dev->irq_base, ch347_dev->irq_num);
}

#endif /* #if defined(CONFIG_CH347_GPIO_IRQ) */

/*************************************************************************************************/

#if defined(CONFIG_CH347_GPIO)

static int ch347_gpio_config_get (struct ch347_device *ch347_dev, u8 id, int *value)
{
	u8 data[CH347_GPIO_NUM];
	int ret;

	memset(data, 0, sizeof(data));

	data[id] |= BIT(7);

	mutex_lock(&ch347_dev->ops_mutex);

	ret = ch347_usb_write(ch347_dev, CH347_USB_CMD_GPIO_CMD, CH347_GPIO_NUM, data);
   	if (ret == 0) {
		ret = ch347_usb_read(ch347_dev, CH347_USB_CMD_NONE, CH347_GPIO_NUM, data);
		if (ret == 0)
			*value = (data[id] & BIT(6)) ? 1 : 0;
	}

	mutex_unlock(&ch347_dev->ops_mutex);

	return ret;
}

static int ch347_gpio_config_set (struct ch347_device *ch347_dev, u8 id, int value)
{
	u8 data[CH347_GPIO_NUM];
	int ret;

	memset(data, 0, sizeof(data));

	data[id] |= BIT(7);
	data[id] |= BIT(6);
	
	if (value == 0 ||  value == 1) /* output */
	{
		data[id] |= BIT(5);
		data[id] |= BIT(4);
		
		if (value == 1)
			data[id] |= BIT(3);
	}

	mutex_lock(&ch347_dev->ops_mutex);

	ret = ch347_usb_write(ch347_dev, CH347_USB_CMD_GPIO_CMD, sizeof(data), data);
   	if (ret == 0) 
		ret = ch347_usb_read(ch347_dev, CH347_USB_CMD_NONE, sizeof(data), NULL);

	mutex_unlock(&ch347_dev->ops_mutex);

	return ret;
}

#if LINUX_VERSION_CODE >= KERNEL_VERSION(3, 8, 0)
static int ch347_gpio_get_direction (struct gpio_chip *chip, unsigned offset)
{
#if LINUX_VERSION_CODE >= KERNEL_VERSION(4, 5, 0)
	struct ch347_device *ch347_dev = (struct ch347_device *)gpiochip_get_data(chip);
#else
	struct ch347_device *ch347_dev = container_of(chip, struct ch347_device, gpio);
#endif
	struct ch347_gpio *gpio;

	CHECK_PARAM_RET(ch347_dev, -EINVAL);
	CHECK_PARAM_RET(offset < ch347_dev->gpio_num, -EINVAL);

	gpio = &ch347_dev->gpio_config[offset];

	DEV_INFO(CH347_USBDEV, "gpio=%d output=%d", gpio->id, gpio->output);

	return gpio->output ? 1 : 0;
}
#endif

static int ch347_gpio_set_direction (struct gpio_chip *chip, unsigned offset, bool output, bool high)
{
#if LINUX_VERSION_CODE >= KERNEL_VERSION(4, 5, 0)
	struct ch347_device *ch347_dev = (struct ch347_device *)gpiochip_get_data(chip);
#else
	struct ch347_device *ch347_dev = container_of(chip, struct ch347_device, gpio);
#endif
	struct ch347_gpio *gpio;
	int value; 

	CHECK_PARAM_RET(ch347_dev, -EINVAL);
	CHECK_PARAM_RET(offset < ch347_dev->gpio_num, -EINVAL);

	gpio = &ch347_dev->gpio_config[offset];
	gpio->output = output;
	value = output ? (high ? 1 : 0) : -1;
		
	DEV_INFO(CH347_USBDEV, "gpio=%d output=%d value=%d", gpio->id, output, value);

	return ch347_gpio_config_set(ch347_dev, gpio->id, value);
}

static int ch347_gpio_direction_input (struct gpio_chip *chip, unsigned int offset)
{
	return ch347_gpio_set_direction(chip, offset, false, true);
}

static int ch347_gpio_direction_output (struct gpio_chip *chip, unsigned int offset, int value)
{
	return ch347_gpio_set_direction(chip, offset, true, !!value);
}

static int ch347_gpio_get (struct gpio_chip *chip, unsigned offset)
{
#if LINUX_VERSION_CODE >= KERNEL_VERSION(4, 5, 0)
	struct ch347_device *ch347_dev = (struct ch347_device *)gpiochip_get_data(chip);
#else
	struct ch347_device *ch347_dev = container_of(chip, struct ch347_device, gpio);
#endif
	struct ch347_gpio *gpio;
	int value;
	int ret;

	CHECK_PARAM_RET(ch347_dev, -EINVAL);
	CHECK_PARAM_RET(offset < ch347_dev->gpio_num, -EINVAL);

	gpio = &ch347_dev->gpio_config[offset];

	ret = ch347_gpio_config_get(ch347_dev, gpio->id, &value);
	if (ret)
		return ret;

	DEV_DBG(CH347_USBDEV, "gpio=%d value=%d", gpio->id, value);

	return value;
}

static void ch347_gpio_set (struct gpio_chip *chip, unsigned offset, int value)
{
#if LINUX_VERSION_CODE >= KERNEL_VERSION(4, 5, 0)
	struct ch347_device *ch347_dev = (struct ch347_device *)gpiochip_get_data(chip);
#else
	struct ch347_device *ch347_dev = container_of(chip, struct ch347_device, gpio);
#endif
	struct ch347_gpio *gpio;

	CHECK_PARAM(ch347_dev);
	CHECK_PARAM(offset < ch347_dev->gpio_num);

	gpio = &ch347_dev->gpio_config[offset];

	DEV_INFO(CH347_USBDEV, "gpio=%d value=%d", gpio->id, value);

	ch347_gpio_config_set(ch347_dev, gpio->id, value);
}

#if defined(CONFIG_CH347_GPIO_IRQ)
static int ch347_gpio_to_irq (struct gpio_chip *chip, unsigned offset)
{
#if LINUX_VERSION_CODE >= KERNEL_VERSION(4, 5, 0)
	struct ch347_device *ch347_dev = (struct ch347_device *)gpiochip_get_data(chip);
#else
	struct ch347_device *ch347_dev = container_of(chip, struct ch347_device, gpio);
#endif
	int irq = -1;

	CHECK_PARAM_RET(ch347_dev, -EINVAL);
	CHECK_PARAM_RET(offset < ch347_dev->gpio_num, -EINVAL);

	irq = ch347_dev->irq_base + offset;

	DEV_DBG(CH347_USBDEV, "gpio=%d irq=%d", offset, irq);

	return irq;
}
#endif

static int ch347_gpio_probe (struct ch347_device *ch347_dev)
{
	struct ch347_gpio *gpio;
	int i;

	CHECK_PARAM_RET(ch347_dev, -EINVAL);

	ch347_dev->gpio_config = ch347f_gpio_config;

	for (i = 0; i < CH347_GPIO_NUM; i++) {
		gpio = ch347_dev->gpio_config + i;
		if (gpio->id < 0)
			break;

		if (i == 0) 
			gpio->irq = true;
		else
			gpio->irq = false;

		gpio->output = gpio->irq ? false : true;

		ch347_gpio_config_set(ch347_dev, gpio->id, gpio->output ? 1 : -1);

		DEV_INFO(CH347_USBDEV, "(%d) GPIO%d %s %s", gpio->pin, gpio->id,
					gpio->output ? "OUTPUT" : "INTPUT", gpio->irq ? "IRQ" : ""); 
	}
	ch347_dev->gpio_num = i;
#if defined(CONFIG_CH347_GPIO_IRQ)
	ch347_dev->irq_num = 1;
#endif	

	if (ch347_dev->gpio_num > 0)
	{
		struct gpio_chip *gpio_chip = &ch347_dev->gpio_chip;
		int ret;

		gpio_chip->label = "ch347-mpsi-gpio";
#if LINUX_VERSION_CODE >= KERNEL_VERSION(4, 5, 0)
		gpio_chip->parent = &ch347_dev->usb_dev->dev;
#else
		gpio_chip->dev = &ch347_dev->usb_dev->dev;
#endif
		gpio_chip->owner = THIS_MODULE;
		gpio_chip->request = NULL;
		gpio_chip->free = NULL;
		gpio_chip->base = (param_gpio_base >= 0) ? param_gpio_base : -1;
		gpio_chip->ngpio = ch347_dev->gpio_num;
		gpio_chip->can_sleep = 1;
#if LINUX_VERSION_CODE >= KERNEL_VERSION(3, 8, 0)
		gpio_chip->get_direction = ch347_gpio_get_direction;
#endif
		gpio_chip->direction_input = ch347_gpio_direction_input;
		gpio_chip->direction_output = ch347_gpio_direction_output;
		gpio_chip->get = ch347_gpio_get;
		gpio_chip->set = ch347_gpio_set;
#if defined(CONFIG_CH347_GPIO_IRQ)
		gpio_chip->to_irq = ch347_gpio_to_irq;
#endif

#if LINUX_VERSION_CODE >= KERNEL_VERSION(4, 5, 0)
		ret = gpiochip_add_data(gpio_chip, ch347_dev);
#else
		ret = gpiochip_add(gpio_chip);
#endif
		if (ret != 0) {
			DEV_ERR(CH347_USBDEV, "failed to register GPIOs: %d", ret);
			gpio_chip->base = -1;
			return ret;
		}

		DEV_INFO(CH347_USBDEV, "registered GPIOs from %d to %d", 
					gpio_chip->base, gpio_chip->base + gpio_chip->ngpio - 1);
	}

	return 0;
}

static void ch347_gpio_remove (struct ch347_device *ch347_dev)
{
	CHECK_PARAM(ch347_dev);

	if (ch347_dev->gpio_chip.base > 0)
		gpiochip_remove(&ch347_dev->gpio_chip);
}

#endif /* #if defined(CONFIG_CH347_GPIO) */

/*************************************************************************************************/

static int ch347_spi_update_hwcfg (stream_hw_cfgs *hwcfg, mspi_cfgs *spicfg)
{
	/* SPI_Clock_Polarity */
	const u16 SPI_CPOL_Low = 0x0000;
	const u16 SPI_CPOL_High = 0x0002;
		
	/* SPI_Clock_Phase */
	const u16 SPI_CPHA_1Edge = 0x0000;
	const u16 SPI_CPHA_2Edge = 0x0001;

	if (spicfg->imode > 3)
		return -EINVAL;

	if (spicfg->iclock > 7)
		return -EINVAL;

	spicfg->ibyteorder &= 0x01;
	hwcfg->spi_initcfg.spi_firstbit = spicfg->ibyteorder ? 0x00 : 0x80;

	switch (spicfg->imode) {
		case 0:
			hwcfg->spi_initcfg.s_spi_cpha = SPI_CPHA_1Edge;
			hwcfg->spi_initcfg.s_spi_cpol = SPI_CPOL_Low;
			break;
		case 1:
			hwcfg->spi_initcfg.s_spi_cpha = SPI_CPHA_2Edge;
			hwcfg->spi_initcfg.s_spi_cpol = SPI_CPOL_Low;
			break;
		case 2:
			hwcfg->spi_initcfg.s_spi_cpha = SPI_CPHA_1Edge;
			hwcfg->spi_initcfg.s_spi_cpol = SPI_CPOL_High;
			break;
		case 3:
			hwcfg->spi_initcfg.s_spi_cpha = SPI_CPHA_2Edge;
			hwcfg->spi_initcfg.s_spi_cpol = SPI_CPOL_High;
			break;
		default:
			hwcfg->spi_initcfg.s_spi_cpha = SPI_CPHA_2Edge;
			hwcfg->spi_initcfg.s_spi_cpol = SPI_CPOL_High;
			break;
	}

	hwcfg->spi_initcfg.s_spi_cpha = __cpu_to_le16(hwcfg->spi_initcfg.s_spi_cpha);
	hwcfg->spi_initcfg.s_spi_cpol = __cpu_to_le16(hwcfg->spi_initcfg.s_spi_cpol);
	hwcfg->spi_initcfg.spi_baudrate_scale = __cpu_to_le16(spicfg->iclock * 8);
	hwcfg->spi_outdef = spicfg->ispi_out_def;
	hwcfg->spi_rw_interval = spicfg->ispi_rw_interval;

	if (spicfg->cs_polar[0])
		hwcfg->misc_cfg |= 0x80;
	else
		hwcfg->misc_cfg &= ~0x80;

	if (spicfg->cs_polar[1])
		hwcfg->misc_cfg |= 0x40;
	else
		hwcfg->misc_cfg &= ~0x40;

	return 0;
}


static int ch347_spi_setup (struct spi_device *spi)
{
	const u32 clock_freq[8] = { 60000000, 30000000, 15000000, 7500000, 3750000, 1875000, 937500, 468750 };
	struct spi_master *master = spi->master;
	struct ch347_device *ch347_dev = spi_master_get_devdata(master);
	stream_hw_cfgs hwcfg;
	mspi_cfgs spicfg;
	u8 scale;
	u8 data;
	int ret;

	memset(&hwcfg, 0, sizeof(hwcfg));
	memset(&spicfg, 0, sizeof(spicfg));

#if LINUX_VERSION_CODE >= KERNEL_VERSION(3, 12, 0)
	if (spi->max_speed_hz < master->min_speed_hz || spi->max_speed_hz > master->max_speed_hz)
		return -EINVAL;
#else
	if (spi->max_speed_hz < CH347_SPI_FREQ_MIN || spi->max_speed_hz > CH347_SPI_FREQ_MAX)
		return -EINVAL;
#endif

	switch (spi->chip_select) {
		case 0:
		case 1:
			spicfg.ics[spi->chip_select] = 0x80;
			break;
		default:
			return -EINVAL;
	}

	DEV_INFO(CH347_USBDEV, "chip_select=%u max_speed_hz=%u mode=0x%X", 
				spi->chip_select, spi->max_speed_hz, spi->mode);

	CH347_OPS_LOCK();

	spicfg.imode = spi->mode & 0x3;

	scale = spi->max_speed_hz / 468750;
	spicfg.iclock = 7;
	while (scale / 2) {
		spicfg.iclock--;
		scale /= 2;
	}

	DEV_INFO(CH347_USBDEV, "scale=%d, clock=%d (%d)", 
			scale, spicfg.iclock, clock_freq[spicfg.iclock]);

	if (spi->mode & SPI_LSB_FIRST)
		spicfg.ibyteorder = 0;
	else
		spicfg.ibyteorder = 1;

	if (spi->mode & SPI_CS_HIGH)
		spicfg.cs_polar[spi->chip_select] = 1;
	else
		spicfg.cs_polar[spi->chip_select] = 0;

	spicfg.ispi_out_def = 0xFF;
	spicfg.ispi_rw_interval = 0;
	spicfg.iauto_de_cs = 0;
	spicfg.iactive_delay = 0;
	spicfg.ideactive_delay = 0;

 	/*
	 * Get spi setting from hardware 
	 */
	data = 1;
	ret = ch347_usb_write(ch347_dev, CH347_USB_CMD_INFO_RD, 1, &data);
   	if (ret == 0) {
		ret = ch347_usb_read(ch347_dev, CH347_USB_CMD_NONE, sizeof(hwcfg), &hwcfg);
   		if (ret == 0) {
			ret = ch347_spi_update_hwcfg(&hwcfg, &spicfg);
			if (ret == 0) {
				/*
				 * Set spi setting to hardware 
				 */
				ret = ch347_usb_write(ch347_dev, CH347_USB_CMD_SPI_INIT, sizeof(hwcfg), &hwcfg);
				if (ret == 0) {
					data = 0xff;
					ret = ch347_usb_read(ch347_dev, CH347_USB_CMD_SPI_INIT, 1, &data);
					if (ret == 0 &&  data == 0x00) {
						memcpy(&ch347_dev->spicfg, &spicfg, sizeof(spicfg));
						memcpy(&ch347_dev->hwcfg, &hwcfg, sizeof(hwcfg));
					}
				}
			}
		}
	}

	CH347_OPS_UNLOCK();

	return ret;
}

static void ch347_spi_set_cs (struct spi_device *spi, bool enable)
{
	struct ch347_device *ch347_dev = spi_master_get_devdata(spi->master);
	u8 data[2][5];
	int i;

/*	DEV_DBG(CH347_USBDEV, "cs=%d enable=%d", spi->chip_select, enable); */

	if (spi->mode & SPI_NO_CS) {
		DEV_INFO(CH347_USBDEV, "No CS");
		return;
	}

	if (spi->chip_select >= CH347_SPI_CS_NUM) {
		DEV_ERR(CH347_USBDEV, "invalid CS number (%d)", spi->chip_select);
		return;
	}

	CH347_OPS_LOCK();

	memset(data, 0, sizeof(data));

	for (i = 0 ; i < CH347_SPI_CS_NUM ; i++) {
		if (i == spi->chip_select && ch347_dev->spicfg.ics[i] == 0x80)
			data[i][0] |= 0x80; /* CS enable */
		else
			data[i][0] &= 0x7F; /* CS disable */

		if (ch347_dev->spicfg.cs_polar[i] == 1 ? enable : !enable)
			data[i][0] &= 0xBF; /* set CS */
		else
			data[i][0] |= 0x40; /* cancel CS */

		if (ch347_dev->spicfg.iauto_de_cs)
			data[i][0] |= 0x20; /* undo CS0 after operation */
		else
			data[i][0] &= 0xDF; /* keep CS0 */

		data[i][1] = (u8)(ch347_dev->spicfg.iactive_delay & 0xFF);
		data[i][2] = (u8)((ch347_dev->spicfg.iactive_delay >> 8) & 0xFF);
		data[i][3] = (u8)(ch347_dev->spicfg.ideactive_delay & 0xFF);
		data[i][4] = (u8)((ch347_dev->spicfg.ideactive_delay >> 8) & 0xFF);
	}

	ch347_usb_write(ch347_dev, CH347_USB_CMD_SPI_CONTROL, sizeof(data), data);
	
	CH347_OPS_UNLOCK();
}

static int ch347_spi_transfer_one (struct spi_master *ctlr, struct spi_device *spi, struct spi_transfer *transfer)
{
	struct ch347_device *ch347_dev = spi_master_get_devdata(ctlr);
	u8 *tx_buf = (u8 *)transfer->tx_buf;
	u8 *rx_buf = (u8 *)transfer->rx_buf;
	int len_buf = transfer->len;
	int len = 0;
	int ret = 0;
	int i;

/*	DEV_DBG(CH347_USBDEV, "tx_buf=%p rx_buf=%p len=%u", tx_buf, rx_buf, len); */

	CH347_OPS_LOCK();

	if (tx_buf && rx_buf) {
		for (i = 0 ; i < len_buf ; i += len)
		{
#if defined(CH347_USB_BULK_LEN_MAX)
			len = min(len_buf - i, CH347_USB_BULK_LEN_MAX - CH347_USB_HDR_LEN);
#else
			len = len_buf;
#endif

			ret = ch347_usb_write(ch347_dev, CH347_USB_CMD_SPI_RD_WR, len, tx_buf + i);
			if (ret != 0)
				goto spi_transfer_exit;

			ret = ch347_usb_read(ch347_dev, CH347_USB_CMD_SPI_RD_WR, len, rx_buf + i);
			if (ret != 0)
				goto spi_transfer_exit;
		}
	} else if (tx_buf) {
		u8 data;

		for (i = 0 ; i < len_buf ; i += len)
		{
#if defined(CH347_USB_BULK_LEN_MAX)
			len = min(len_buf - i, CH347_USB_BULK_LEN_MAX - CH347_USB_HDR_LEN);
#else
			len = len_buf;
#endif

			ret = ch347_usb_write(ch347_dev, CH347_USB_CMD_SPI_BLCK_WR, len, tx_buf + i);
			if (ret != 0)
				goto spi_transfer_exit;

			data = 0xff;
			ret = ch347_usb_read(ch347_dev, CH347_USB_CMD_NONE, 1, &data);
			if (ret != 0)
				goto spi_transfer_exit;

			if (data != 0x00) {
				ret = -EPROTO;
				goto spi_transfer_exit;
			}
		}
	} else if (rx_buf) {
		for (i = 0 ; i < len_buf ; i += len)
		{
#if defined(CH347_USB_BULK_LEN_MAX)
			len = min(len_buf - i, CH347_USB_BULK_LEN_MAX - CH347_USB_HDR_LEN);
#else
			len = len_buf;
#endif

			ret = ch347_usb_write(ch347_dev, CH347_USB_CMD_SPI_BLCK_RD, sizeof(u32), &len);
			if (ret == 0) {
				ret = ch347_usb_read(ch347_dev, CH347_USB_CMD_SPI_BLCK_RD, len, rx_buf + i);
				if (ret != 0)
					goto spi_transfer_exit;
			}
		}
	}
	else
		ret = -EINVAL;
	
spi_transfer_exit:

	CH347_OPS_UNLOCK();

/*	DEV_DBG(CH347_USBDEV, "ret=%d", ret); */

	return ret;
}

static int ch347_spi_probe (struct ch347_device *ch347_dev)
{
	struct platform_device *pdev = NULL;
	struct spi_master *master = NULL;
	int ret;

	pdev = platform_device_alloc("spi-ch347", 0);
	if (!pdev) {
		DEV_ERR(CH347_USBDEV, "platform_device_alloc() failed");
		ret = -ENOMEM;
		goto spi_probe_exit;
	}

	pdev->dev.parent = &ch347_dev->intf->dev;
#if LINUX_VERSION_CODE >= KERNEL_VERSION(4, 1, 0)
	pdev->dev.fwnode = NULL;
#endif
	pdev->id = ch347_dev->id;

	ret = platform_device_add_data(pdev, NULL, 0);
	if (ret < 0) {
		DEV_ERR(CH347_USBDEV, "platform_device_add_data() failed");
		goto spi_probe_exit;
	}

	ret = platform_device_add(pdev);
	if (ret < 0) {
		DEV_ERR(CH347_USBDEV, "platform_device_add() failed");
		goto spi_probe_exit;
	}

	master = spi_alloc_master(CH347_USBDEV, sizeof(struct ch347_device *));
	if (!master) {
		DEV_ERR(CH347_USBDEV, "spi_alloc_master() failed");
		ret = -ENOMEM;
		goto spi_probe_exit;
	}

	platform_set_drvdata(pdev, ch347_dev);
	spi_master_set_devdata(master, ch347_dev);

	/* SPI master configuration */
	master->bus_num = (param_bus_num >= 0) ? param_bus_num : -1;
	master->num_chipselect = CH347_SPI_CS_NUM;
	master->mode_bits = SPI_CPOL | SPI_CPHA | SPI_LSB_FIRST | SPI_CS_HIGH;
#if LINUX_VERSION_CODE >= KERNEL_VERSION(3, 15, 0)
	master->flags = SPI_MASTER_MUST_RX | SPI_MASTER_MUST_TX;
#endif
#if LINUX_VERSION_CODE > KERNEL_VERSION(5, 0, 0)
	master->bits_per_word_mask = SPI_BPW_MASK(8);
#elif LINUX_VERSION_CODE >= KERNEL_VERSION(3, 11, 0)
	master->bits_per_word_mask = SPI_BIT_MASK(8);
#endif
#if LINUX_VERSION_CODE >= KERNEL_VERSION(3, 12, 0)
	master->max_speed_hz = CH347_SPI_FREQ_MAX;
	master->min_speed_hz = CH347_SPI_FREQ_MIN;
#endif
	master->setup = ch347_spi_setup;
	master->set_cs = ch347_spi_set_cs;
	master->transfer_one = ch347_spi_transfer_one;

	ret = spi_register_master(master);
   	if (ret < 0) {
		DEV_ERR(CH347_USBDEV, "spi_register_master() failed");
		goto spi_probe_exit;
	}

	DEV_INFO(CH347_USBDEV, "SPI master connected to SPI bus %d", master->bus_num);

	ch347_dev->spi_pdev = pdev;
	ch347_dev->spi_master = master;

	return 0;

spi_probe_exit:

	if (master)
		spi_master_put(master);

	if (pdev)
		platform_device_put(pdev);

	return ret;
}

static void ch347_spi_remove (struct ch347_device *ch347_dev)
{
	if (ch347_dev->spi_master)
		spi_unregister_master(ch347_dev->spi_master);

	if (ch347_dev->spi_pdev)
		platform_device_unregister(ch347_dev->spi_pdev);
}

/*************************************************************************************************/

#if !defined(DEBUG)
static int ch347_usb_read (struct ch347_device *ch347_dev, u8 cmd, u16 len, void *data)
#else
static int _ch347_usb_read (struct ch347_device *ch347_dev, u8 cmd, u16 len, void *data, const char *caller)
#endif
{
	struct usb_device *usb_dev = ch347_dev->usb_dev;
	unsigned int pipe = usb_rcvbulkpipe(usb_dev, usb_endpoint_num(ch347_dev->bulk_in));
	char *buf = ch347_dev->bulkin_buf;
	int actual_length = 0;
	int ret;

	DEV_DBG(CH347_USBDEV, "cmd=0x%X len=%u data=%p", cmd, len, data); 

	CH347_IO_LOCK();

	len += CH347_USB_HDR_LEN;
	memset(buf, 0, len);

	ret = usb_bulk_msg(usb_dev, pipe, buf, len, &actual_length, param_usb_timeout);

	if (ret == 0) {
		if (actual_length != len)
			ret = -EPROTO;
		else {
			union {
				u16 val;
				struct {
					u8 low;
					u8 high;
				};
			} data_len;

			data_len.low = buf[1];
			data_len.high = buf[2];

			if (cmd != CH347_USB_CMD_NONE && cmd != buf[0])
				ret = -EPROTO;

			if (data_len.val != (len - CH347_USB_HDR_LEN)) 
				ret = -EPROTO;
		
			if (ret != 0)
				DEV_ERR(CH347_USBDEV, "invalid data, cmd=0x%X len=%d", buf[0], data_len.val);
		} 
	}
	
	if (ret < 0) {
#if !defined(DEBUG)
		DEV_ERR(CH347_USBDEV, "cmd=0x%X len=%d/%d ret=%d timeout=%d", cmd, actual_length, len, ret, param_usb_timeout);
#else		
		DEV_ERR(CH347_USBDEV, "cmd=0x%X len=%d/%d ret=%d timeout=%d caller=%s", cmd, actual_length, len, ret, param_usb_timeout, caller);
#endif	
	}
	else if (data)
		memcpy(data, buf + CH347_USB_HDR_LEN, len - CH347_USB_HDR_LEN);

	CH347_IO_UNLOCK();

	HEX_DUMP("usb_read ", buf, actual_length, true); 

	return ret;
}

#if !defined(DEBUG)
static int ch347_usb_write (struct ch347_device *ch347_dev, u8 cmd, u16 len, void *data)
#else
static int _ch347_usb_write (struct ch347_device *ch347_dev, u8 cmd, u16 len, void *data, const char *caller)
#endif
{
	struct usb_device *usb_dev = ch347_dev->usb_dev;
	unsigned int pipe = usb_sndbulkpipe(usb_dev, usb_endpoint_num(ch347_dev->bulk_out));
	char *buf = ch347_dev->bulkout_buf;
	int actual_length = 0;
	int ret;

	DEV_DBG(CH347_USBDEV, "cmd=0x%X len=%u data=%p", cmd, len, data); 

	CH347_IO_LOCK();

	buf[0] = cmd;
	buf[1] = (u8)(len & 0xff);
	buf[2] = (u8)((len >> 8) & 0xff);
	memcpy(buf + CH347_USB_HDR_LEN, data, len);
	
	len += CH347_USB_HDR_LEN;

	ret = usb_bulk_msg(usb_dev, pipe, buf, len, &actual_length, param_usb_timeout);

	if (ret == 0 && actual_length != len)
		ret = -EPROTO;
	
	if (ret < 0) {
#if !defined(DEBUG)
		DEV_ERR(CH347_USBDEV, "cmd=0x%X len=%d/%d ret=%d timeout=%d", cmd, actual_length, len, ret, param_usb_timeout);
#else		
		DEV_ERR(CH347_USBDEV, "cmd=0x%X len=%d/%d ret=%d timeout=%d caller=%s", cmd, actual_length, len, ret, param_usb_timeout, caller);
#endif		
	}

	CH347_IO_UNLOCK();

	HEX_DUMP("usb_write ", buf, actual_length, true); 

	return ret;
}

static void ch347_usb_free_device (struct ch347_device *ch347_dev)
{
	CHECK_PARAM(ch347_dev)

}

static int ch347_usb_probe (struct usb_interface *intf, const struct usb_device_id *id)
{
	struct usb_device *usb_dev = usb_get_dev(interface_to_usbdev(intf));
	struct usb_endpoint_descriptor *endpoint;
	struct usb_host_interface *iface_desc;
	struct ch347_device *ch347_dev;
	int i;
	int ret;

	ch347_dev = kzalloc(sizeof(struct ch347_device), GFP_KERNEL);
	if (!ch347_dev) {
		DEV_ERR(&intf->dev, "could not allocate device memor");
		return -ENOMEM;
	}

	ch347_dev->usb_dev = usb_dev;
	ch347_dev->intf = intf;
	iface_desc = intf->cur_altsetting;

	DEV_DBG(CH347_USBDEV, "bNumEndpoints=%d", iface_desc->desc.bNumEndpoints);

	for (i = 0; i < iface_desc->desc.bNumEndpoints; i++) {
		endpoint = &iface_desc->endpoint[i].desc;

		DEV_DBG(CH347_USBDEV, "  endpoint=%d type=%d dir=%d addr=%0x", i, usb_endpoint_type(endpoint),
			usb_endpoint_dir_in(endpoint), usb_endpoint_num(endpoint));

		if (usb_endpoint_is_bulk_in(endpoint))
			ch347_dev->bulk_in = endpoint;
		else if (usb_endpoint_is_bulk_out(endpoint))
			ch347_dev->bulk_out = endpoint;
		else if (usb_endpoint_xfer_int(endpoint))
			ch347_dev->intr_in = endpoint;
	}

	/* save the pointer to the new ch347_device in USB interface device data */
	usb_set_intfdata(intf, ch347_dev);
	mutex_init(&ch347_dev->io_mutex);
	mutex_init(&ch347_dev->ops_mutex);

	switch (id->idProduct) {
		case 0x55DB: 
			DEV_INFO(CH347_USBDEV, "CH347T SPI (Mode 1)");
			ch347_dev->chiptype = CHIP_CH347T; 
			break;

		case 0x55DE: 
#if defined(CONFIG_CH347_GPIO)
			DEV_INFO(CH347_USBDEV, "CH347F SPI+GPIO");
#else
			DEV_INFO(CH347_USBDEV, "CH347F SPI");
#endif
			ch347_dev->chiptype = CHIP_CH347F; 
			break;

		default:
			return -ENODEV;
	}

	ch347_dev->id = ida_simple_get(&ch347_devid_ida, 0, 0, GFP_KERNEL);
	if (ch347_dev->id < 0)
		return ch347_dev->id;

	ret = ch347_spi_probe(ch347_dev);
	if (ret < 0)
		goto usb_probe_exit;

	if (ch347_dev->chiptype == CHIP_CH347F) {
#if defined(CONFIG_CH347_GPIO)
		ret = ch347_gpio_probe(ch347_dev);
		if (ret < 0)
			goto usb_probe_exit;
#endif

#if defined(CONFIG_CH347_GPIO_IRQ)
		ret = ch347_irq_probe(ch347_dev);
		if (ret < 0)
			goto usb_probe_exit;
#endif	
	}

	return 0;

usb_probe_exit:

	ch347_spi_remove(ch347_dev);
	ida_simple_remove(&ch347_devid_ida, ch347_dev->id);
	ch347_usb_free_device(ch347_dev);

	return ret;
}

static void ch347_usb_disconnect (struct usb_interface *intf)
{
	struct ch347_device *ch347_dev = usb_get_intfdata(intf);

	if (ch347_dev->chiptype == CHIP_CH347F) {
#if defined(CONFIG_CH347_GPIO_IRQ)
		ch347_irq_remove(ch347_dev);
#endif	
#if defined(CONFIG_CH347_GPIO)
		ch347_gpio_remove(ch347_dev);
#endif
	}
	ch347_spi_remove(ch347_dev);
	
	usb_set_intfdata(ch347_dev->intf, NULL);
	usb_put_dev(ch347_dev->usb_dev);

	ida_simple_remove(&ch347_devid_ida, ch347_dev->id);

	kfree(ch347_dev);
}

static struct usb_driver ch347_usb_driver = { 
	.name = "spi-ch347",
	.id_table = ch347_usb_ids,
	.probe = ch347_usb_probe,
	.disconnect = ch347_usb_disconnect 
};

module_usb_driver(ch347_usb_driver);

MODULE_ALIAS("SPI/GPIO: CH347F/CH347T");
MODULE_AUTHOR("WCH");
MODULE_DESCRIPTION("USB to SPI/GPIO driver for CH347F/CH347T");
MODULE_LICENSE("GPL");

