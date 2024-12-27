/*
 * Copyright (c) 2016-2019 Newracom, Inc.
 *
 * Permission to use, copy, modify, and/or distribute this software for any
 * purpose with or without fee is hereby granted, provided that the above
 * copyright notice and this permission notice appear in all copies.
 *
 * THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
 * WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
 * MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
 * ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
 * WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
 * ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
 * OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
 */

#ifndef __COMPAT_H__
#define __COMPAT_H__

#include <net/mac80211.h>
#include "nrc.h"
#include "wim.h"

#if !defined(ieee80211_hw_set)
#define ieee80211_hw_set(hw, flg) ((hw)->flags |= IEEE80211_HW_##flg)
#endif

#if !defined(CONFIG_SUPPORT_BEACON_TEMPLATE)
#define ieee80211_beacon_get_template(hw, vif, N) ieee80211_beacon_get(hw, vif)
#endif

#if KERNEL_VERSION(4, 8, 16) >= NRC_TARGET_KERNEL_VERSION

/* from bug.h */
#ifdef __CHECKER__
#define __BUILD_BUG_ON_NOT_POWER_OF_2(n) (0)
#else
#define __BUILD_BUG_ON_NOT_POWER_OF_2(n)    \
    BUILD_BUG_ON(((n) & ((n) - 1)) != 0)
#endif

/* from linux/bitfield.h */
#define __bf_shf(x) (__builtin_ffsll(x) - 1)

#define __BF_FIELD_CHECK(_mask, _reg, _val, _pfx)           \
    ({                              \
        BUILD_BUG_ON_MSG(!__builtin_constant_p(_mask),      \
                 _pfx "mask is not constant");      \
        BUILD_BUG_ON_MSG(!(_mask), _pfx "mask is zero");    \
        BUILD_BUG_ON_MSG(__builtin_constant_p(_val) ?       \
                 ~((_mask) >> __bf_shf(_mask)) & (_val) : 0, \
                 _pfx "value too large for the field"); \
        BUILD_BUG_ON_MSG((_mask) > (typeof(_reg))~0ull,     \
                 _pfx "type of reg too small for mask"); \
        __BUILD_BUG_ON_NOT_POWER_OF_2((_mask) +         \
                          (1ULL << __bf_shf(_mask))); \
    })

#define FIELD_PREP(_mask, _val)                     \
    ({                              \
        __BF_FIELD_CHECK(_mask, 0ULL, _val, "FIELD_PREP: ");    \
        ((typeof(_mask))(_val) << __bf_shf(_mask)) & (_mask);   \
    })

#define FIELD_GET(_mask, _reg)                      \
    ({                              \
        __BF_FIELD_CHECK(_mask, _reg, 0U, "FIELD_GET: ");   \
        (typeof(_mask))(((_reg) & (_mask)) >> __bf_shf(_mask)); \
    })

#endif


/* this function is added in 4.15.0, but in rpi 4.15.18, no exist */
//#if KERNEL_VERSION(4, 15, 0) > NRC_TARGET_KERNEL_VERSION
#if KERNEL_VERSION(4, 16, 0) > NRC_TARGET_KERNEL_VERSION
#include <asm/byteorder.h>

extern void __compiletime_error("value doesn't fit into mask")
__field_overflow(void);
extern void __compiletime_error("bad bitfield mask")
__bad_mask(void);
static __always_inline u64 field_multiplier(u64 field)
{
	if ((field | (field - 1)) & ((field | (field - 1)) + 1))
		__bad_mask();
	return field & -field;
}
static __always_inline u64 field_mask(u64 field)
{
	return field / field_multiplier(field);
}
#define field_max(field)	((typeof(field))field_mask(field))
#define ____MAKE_OP(type,base,to,from)					\
static __always_inline __##type type##_encode_bits(base v, base field)	\
{									\
	if (__builtin_constant_p(v) && (v & ~field_mask(field)))	\
		__field_overflow();					\
	return to((v & field_mask(field)) * field_multiplier(field));	\
}									\
static __always_inline __##type type##_replace_bits(__##type old,	\
					base val, base field)		\
{									\
	return (old & ~to(field)) | type##_encode_bits(val, field);	\
}									\
static __always_inline void type##p_replace_bits(__##type *p,		\
					base val, base field)		\
{									\
	*p = (*p & ~to(field)) | type##_encode_bits(val, field);	\
}									\
static __always_inline base type##_get_bits(__##type v, base field)	\
{									\
	return (from(v) & field)/field_multiplier(field);		\
}
#define __MAKE_OP(size)							\
	____MAKE_OP(le##size,u##size,cpu_to_le##size,le##size##_to_cpu)	\
	____MAKE_OP(be##size,u##size,cpu_to_be##size,be##size##_to_cpu)	\
	____MAKE_OP(u##size,u##size,,)
____MAKE_OP(u8,u8,,)
__MAKE_OP(16)
__MAKE_OP(32)
__MAKE_OP(64)
#undef __MAKE_OP
#undef ____MAKE_OP

#endif

#if KERNEL_VERSION(4, 12, 14) >= NRC_TARGET_KERNEL_VERSION
static inline void *skb_put_zero(struct sk_buff *skb, unsigned int len)
{
	void *tmp = skb_put(skb, len);

	memset(tmp, 0, len);

	return tmp;
}
#endif

#endif
