/* Minimal helper declarations for the HPFT TCP EDT prototype. */
#ifndef HPFT_BPF_HELPERS_H
#define HPFT_BPF_HELPERS_H

#include <linux/bpf.h>

#define SEC(NAME) __attribute__((section(NAME), used))
#define __uint(NAME, VAL) int (*NAME)[VAL]
#define __type(NAME, VAL) typeof(VAL) *NAME

#ifndef __always_inline
#define __always_inline inline __attribute__((always_inline))
#endif

#ifndef BPF_ANY
#define BPF_ANY 0
#endif

#ifndef TC_ACT_OK
#define TC_ACT_OK 0
#endif

static void *(*bpf_map_lookup_elem)(void *map, const void *key) = (void *)1;
static __u64 (*bpf_ktime_get_ns)(void) = (void *)5;
static void (*bpf_spin_lock)(struct bpf_spin_lock *lock) = (void *)93;
static void (*bpf_spin_unlock)(struct bpf_spin_lock *lock) = (void *)94;

#endif

