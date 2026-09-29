// chmgrd - channel manager daemon for NRC7394 host mode.
//
// The firmware decides when the AP should leave its channel. It sends that
// request to the driver, which broadcasts it on a generic-netlink multicast
// group ("nrc-chmgr"). chmgrd waits on that group and performs the channel
// switch by running hostapd_cli, so the AP announces it with an ECSA. It
// never polls the firmware.
//
// Usage: chmgrd -i <interface> [-l <logfile>]

#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include <ctype.h>
#include <unistd.h>
#include <time.h>
#include <errno.h>
#include <fcntl.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <poll.h>
#include <signal.h>
#include <linux/netlink.h>
#include <linux/genetlink.h>

#define FAMILY_NAME     "NRC-NL-FAM"
#define MCGRP_NAME      "nrc-chmgr"
#define NL_CMD_DCS_SWITCH   22      /* must match nrc-netlink.h */
#define NL_DCS_CHANNEL      40      /* must match nrc-netlink.h */
#define NL_DCS_VIF          41      /* must match nrc-netlink.h */
#define RETRY_SEC           2       /* setup retry backoff */
#define CLI_TMO_MS          5000    /* shorter than the FW CHMGR_SWITCH_TMO_MS */

#define GENLMSG_DATA(glh)   ((void *)((char *)(glh) + NLMSG_HDRLEN + GENL_HDRLEN))
#define NLA_DATA(na)        ((void *)((char *)(na) + NLA_HDRLEN))
#ifndef SOL_NETLINK
#define SOL_NETLINK 270
#endif

struct msg {
	struct nlmsghdr n;
	struct genlmsghdr g;
	char buf[1024];
};

static char  g_iface[32] = "wlan0";
static char  g_log[128]  = "/tmp/chmgrd.log";
static FILE *g_logf;

static void logmsg(const char *fmt, ...)
{
	time_t t = time(NULL);
	struct tm *tm = localtime(&t);
	char ts[32];
	va_list ap;

	/* a log file we could not open used to silence every message, so a
	 * hand-started chmgrd told the operator nothing at all */
	FILE *out = g_logf ? g_logf : stderr;

	if (tm && strftime(ts, sizeof(ts), "%m-%d %H:%M:%S", tm) > 0)
		fprintf(out, "%s ", ts);
	va_start(ap, fmt);
	vfprintf(out, fmt, ap);
	va_end(ap);
	fprintf(out, "\n");
	fflush(out);
}

// Reject interface names that are not plain tokens (defense in depth; the
// name is also never passed through a shell any more).
static int iface_ok(const char *s)
{
	if (!*s || strlen(s) >= sizeof(g_iface))
		return 0;
	for (; *s; s++)
		if (!isalnum((unsigned char)*s) && *s != '-' && *s != '_' && *s != '.')
			return 0;
	return 1;
}

// Proxy (non-S1G) channel number -> MHz, matching the driver/hostapd scheme.
// Returns 0 for a channel with no valid mapping (caller skips the switch).
// NOTE: S1G-native builds (CONFIG_S1G_CHANNEL) would need an S1G table here.
static int proxy_to_freq(int ch)
{
	if (ch >= 1 && ch <= 14)
		return 2407 + 5 * ch;
	if (ch >= 32 && ch <= 196)
		return 5000 + 5 * ch;
	return 0;
}

// Resolve a multicast group id by (family, group) name via CTRL_CMD_GETFAMILY.
static int resolve_group(int fd, const char *family, const char *group,
                         __u32 *grp_id)
{
	struct msg req, rsp;
	struct nlattr *na;
	struct sockaddr_nl sa;
	int len, rem;

	memset(&req, 0, sizeof(req));
	req.n.nlmsg_len   = NLMSG_LENGTH(GENL_HDRLEN);
	req.n.nlmsg_type  = GENL_ID_CTRL;
	req.n.nlmsg_flags = NLM_F_REQUEST;
	req.n.nlmsg_pid   = getpid();
	req.g.cmd         = CTRL_CMD_GETFAMILY;
	req.g.version     = 1;

	na = (struct nlattr *) GENLMSG_DATA(&req);
	na->nla_type = CTRL_ATTR_FAMILY_NAME;
	na->nla_len  = strlen(family) + 1 + NLA_HDRLEN;
	strcpy(NLA_DATA(na), family);
	req.n.nlmsg_len += NLMSG_ALIGN(na->nla_len);

	memset(&sa, 0, sizeof(sa));
	sa.nl_family = AF_NETLINK;
	if (sendto(fd, &req, req.n.nlmsg_len, 0,
	           (struct sockaddr *) &sa, sizeof(sa)) < 0)
		return -1;

	len = recv(fd, &rsp, sizeof(rsp), 0);
	if (len < 0 || !NLMSG_OK(&rsp.n, len) || rsp.n.nlmsg_type == NLMSG_ERROR)
		return -1;

	*grp_id = 0;
	na = (struct nlattr *) GENLMSG_DATA(&rsp);
	rem = len - (NLMSG_HDRLEN + GENL_HDRLEN);
	for (; rem >= (int) NLA_HDRLEN && na->nla_len >= NLA_HDRLEN && rem >= na->nla_len;
	     rem -= NLMSG_ALIGN(na->nla_len),
	     na = (struct nlattr *) ((char *) na + NLMSG_ALIGN(na->nla_len))) {
		if (na->nla_type != CTRL_ATTR_MCAST_GROUPS)
			continue;
		struct nlattr *grp = (struct nlattr *) NLA_DATA(na);
		int grem = na->nla_len - NLA_HDRLEN;
		for (; grem >= (int) NLA_HDRLEN && grp->nla_len >= NLA_HDRLEN && grem >= grp->nla_len;
		     grem -= NLMSG_ALIGN(grp->nla_len),
		     grp = (struct nlattr *) ((char *) grp + NLMSG_ALIGN(grp->nla_len))) {
			struct nlattr *f = (struct nlattr *) NLA_DATA(grp);
			int frem = grp->nla_len - NLA_HDRLEN;
			__u32 id = 0;
			const char *name = NULL;
			for (; frem >= (int) NLA_HDRLEN && f->nla_len >= NLA_HDRLEN && frem >= f->nla_len;
			     frem -= NLMSG_ALIGN(f->nla_len),
			     f = (struct nlattr *) ((char *) f + NLMSG_ALIGN(f->nla_len))) {
				if (f->nla_type == CTRL_ATTR_MCAST_GRP_ID &&
				    f->nla_len >= NLA_HDRLEN + sizeof(__u32))
					id = *(__u32 *) NLA_DATA(f);
				else if (f->nla_type == CTRL_ATTR_MCAST_GRP_NAME)
					name = (const char *) NLA_DATA(f);
			}
			if (name && !strcmp(name, group))
				*grp_id = id;
		}
	}
	return *grp_id ? 0 : -1;
}

// Build the socket and join nrc-chmgr (required) + genl notify (best effort,
// for module-reload detection). Returns fd on success, -1 otherwise.
static int setup_socket(void)
{
	struct sockaddr_nl sa;
	__u32 grp, notify;
	int fd;

	fd = socket(AF_NETLINK, SOCK_RAW, NETLINK_GENERIC);
	if (fd < 0) {
		logmsg("socket(NETLINK_GENERIC) failed: %s", strerror(errno));
		return -1;
	}
	memset(&sa, 0, sizeof(sa));
	sa.nl_family = AF_NETLINK;
	/* name the step that failed: a bare retry loop left the operator with
	 * a live process, an empty log and no idea which end was at fault */
	if (bind(fd, (struct sockaddr *) &sa, sizeof(sa)) < 0) {
		logmsg("bind failed: %s", strerror(errno));
		goto fail;
	}
	if (resolve_group(fd, FAMILY_NAME, MCGRP_NAME, &grp) < 0) {
		logmsg("family %s group %s not found -- is the driver loaded?",
		       FAMILY_NAME, MCGRP_NAME);
		goto fail;
	}
	if (setsockopt(fd, SOL_NETLINK, NETLINK_ADD_MEMBERSHIP, &grp, sizeof(grp)) < 0) {
		logmsg("joining group %u failed: %s", grp, strerror(errno));
		goto fail;
	}
	/* watch genl family add/remove so a driver reload is picked up */
	if (resolve_group(fd, "nlctrl", "notify", &notify) == 0)
		setsockopt(fd, SOL_NETLINK, NETLINK_ADD_MEMBERSHIP, &notify, sizeof(notify));
	logmsg("chmgrd (event-driven) start iface=%s grp=%u", g_iface, grp);
	return fd;
fail:
	close(fd);
	return -1;
}

/* run hostapd_cli with av and collect its output. -1 only when the child
 * could not be started; a hostapd that answered an error still returns 0. */
static int run_cli(char *const av[], char *out, size_t outsz)
{
	int pfd[2], tot = 0, n, status;
	pid_t pid;

	out[0] = '\0';
	if (pipe(pfd) < 0) {
		logmsg("pipe() failed");
		return -1;
	}
	pid = fork();
	if (pid < 0) {
		logmsg("fork() failed");
		close(pfd[0]);
		close(pfd[1]);
		return -1;
	}
	if (pid == 0) {                         /* child: exec, no shell */
		static const char *const dirs[] = { "/usr/local/bin", "/usr/sbin",
		                                    "/sbin", "/usr/bin", "/bin" };
		char path[64];
		unsigned d;

		dup2(pfd[1], STDOUT_FILENO);
		dup2(pfd[1], STDERR_FILENO);
		close(pfd[0]);
		close(pfd[1]);
		/* fixed list, not execvp: what this daemon runs must not depend on
		 * the PATH it happened to inherit */
		for (d = 0; d < sizeof(dirs) / sizeof(dirs[0]); d++) {
			snprintf(path, sizeof(path), "%s/hostapd_cli", dirs[d]);
			execv(path, av);
		}
		_exit(127);
	}
	close(pfd[1]);
	/* a hostapd_cli that never closes its pipe would block this loop, and
	 * with it every later switch request: wait bounded and kill the child */
	{
		struct pollfd pf = { .fd = pfd[0], .events = POLLIN };

		while (tot < (int) outsz - 1) {
			if (poll(&pf, 1, CLI_TMO_MS) <= 0) {
				logmsg("hostapd_cli no output in %d ms -- killing",
				       CLI_TMO_MS);
				kill(pid, SIGKILL);
				break;
			}
			n = read(pfd[0], out + tot, outsz - 1 - tot);
			if (n <= 0)
				break;                  /* EOF */
			tot += n;
		}
	}
	out[tot > 0 ? tot : 0] = '\0';
	close(pfd[0]);
	waitpid(pid, &status, 0);
	return 0;
}

/* hostapd answers on one line; keep the log to that */
static char *first_line(char *s)
{
	char *nl = strchr(s, '\n');

	if (nl)
		*nl = '\0';
	return s[0] ? s : (char *) "(no output)";
}

/* the netlink group is readable unprivileged, hostapd's control socket is
 * not. Ask once at startup, so a daemon that can never switch says so
 * instead of sitting there looking healthy. */
static void check_ctrl(void)
{
	char *av[] = { "hostapd_cli", "-i", g_iface, "ping", NULL };
	char out[512];

	if (run_cli(av, out, sizeof(out)) < 0 || strstr(out, "PONG"))
		return;
	logmsg("cannot reach hostapd on %s: %s", g_iface, first_line(out));
	if (geteuid() != 0)
		logmsg("not running as root -- run under sudo or no switch "
		       "request will take effect");
}

static void do_switch(int proxy_ch, int vif)
{
	int freq = proxy_to_freq(proxy_ch);
	char freqs[16], out[512];
	char *av[] = { "hostapd_cli", "-i", g_iface,
	               "chan_switch", "10", freqs, NULL };

	if (freq == 0) {
		logmsg("DCS_SWITCH_REQ proxy=%d vif=%d -> no freq mapping, ignored",
		       proxy_ch, vif);
		return;
	}
	logmsg("DCS_SWITCH_REQ proxy=%d vif=%d -> chan_switch %d MHz",
	       proxy_ch, vif, freq);
	snprintf(freqs, sizeof(freqs), "%d", freq);

	if (run_cli(av, out, sizeof(out)) < 0)
		return;
	if (strstr(out, "OK"))
		logmsg("chan_switch OK (%d MHz)", freq);
	else
		/* hostapd's reason is the only thing that says why, and dropping
		 * it left "FAILED" meaning anything from EBUSY to a dead socket */
		logmsg("chan_switch FAILED (%d MHz): %s", freq, first_line(out));
}

int main(int argc, char **argv)
{
	int fd, opt, lfd;

	while ((opt = getopt(argc, argv, "i:l:")) != -1) {
		switch (opt) {
		case 'i': snprintf(g_iface, sizeof(g_iface), "%s", optarg); break;
		case 'l': snprintf(g_log, sizeof(g_log), "%s", optarg); break;
		}
	}
	/* open the log once, refusing to follow a symlink (root writer) */
	lfd = open(g_log, O_WRONLY | O_CREAT | O_APPEND | O_NOFOLLOW, 0644);
	if (lfd >= 0)
		g_logf = fdopen(lfd, "a");
	else
		fprintf(stderr, "chmgrd: cannot open %s (%s) -- logging to stderr\n",
		        g_log, strerror(errno));

	if (!iface_ok(g_iface)) {
		logmsg("bad interface name, refusing to start");
		return 1;
	}
	check_ctrl();

	for (;;) {                               /* reconnect loop */
		fd = setup_socket();
		if (fd < 0) {
			sleep(RETRY_SEC);            /* boot race / driver not up yet */
			continue;
		}

		for (;;) {                       /* recv loop */
			struct msg m;
			int len = recv(fd, &m, sizeof(m), 0);   /* blocks; no poll */

			if (len < 0) {
				if (errno == EINTR)
					continue;
				logmsg("recv error (%d) -- reconnecting", errno);
				break;               /* rebuild the socket */
			}
			if (!NLMSG_OK(&m.n, len) || m.n.nlmsg_type == NLMSG_ERROR)
				continue;
			/* genl controller notified a family add/remove: the driver
			 * may have reloaded, so rebuild and re-resolve the group */
			if (m.n.nlmsg_type == GENL_ID_CTRL) {
				if (m.g.cmd == CTRL_CMD_NEWFAMILY ||
				    m.g.cmd == CTRL_CMD_DELFAMILY) {
					logmsg("genl family changed -- re-resolving");
					break;
				}
				continue;
			}
			if (m.g.cmd != NL_CMD_DCS_SWITCH)
				continue;

			/* NOTE: a switch event already in flight when the operator
			 * runs `set chmgr off` is still executed (bounded race: at
			 * most one stale switch; hold-down + re-arm correct it). */
			struct nlattr *na = (struct nlattr *) GENLMSG_DATA(&m);
			int rem = len - (NLMSG_HDRLEN + GENL_HDRLEN);
			int ch = -1, vif = 0;

			for (; rem >= (int) NLA_HDRLEN && na->nla_len >= NLA_HDRLEN &&
			     rem >= na->nla_len;
			     rem -= NLMSG_ALIGN(na->nla_len),
			     na = (struct nlattr *) ((char *) na + NLMSG_ALIGN(na->nla_len))) {
				if (na->nla_type == NL_DCS_CHANNEL &&
				    na->nla_len >= NLA_HDRLEN + (int) sizeof(__u16))
					ch = *(__u16 *) NLA_DATA(na);
				else if (na->nla_type == NL_DCS_VIF &&
				         na->nla_len >= NLA_HDRLEN + (int) sizeof(__u16))
					vif = *(__u16 *) NLA_DATA(na);
			}
			if (ch >= 0)
				do_switch(ch, vif);
		}
		close(fd);
	}
	/* not reached */
}
