/*
 * SPDX-FileCopyrightText: 2020-2026 Newracom, Inc.
 *
 * SPDX-License-Identifier: MIT
 */

/*
 * MIT License
 *
 * Copyright (c) 2020 Newracom, Inc.
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 *
 */

#ifndef _CLI_NETLINK_
#define _CLI_NETLINK_

#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <errno.h>
#include <linux/genetlink.h>

#include "cli_cmd.h"
#include "cli_util.h"
#include "cli_netlink.h"

#define NL_MSG_BUFFER_SIZE	1024
#define NL_TID_BYTE_SIZE	1
#define NL_MAC_ADDRESS_BYTE_SIZE	6

#define GENLMSG_DATA(glh) ((void *)(NLMSG_DATA(glh) + GENL_HDRLEN))
#define GENLMSG_PAYLOAD(glh) (NLMSG_PAYLOAD(glh, 0) - GENL_HDRLEN)
#define NLA_DATA(na) ((void *)((char*)(na) + NLA_HDRLEN))

typedef struct nl_msg{ //memory for netlink request and response messages - headers are included
	struct nlmsghdr n;
	struct genlmsghdr g;
	char buf[NL_MSG_BUFFER_SIZE];
} nl_msg_t;

int netlink_send_data_with_retry(char cmd_type, char* param, char* response, int retry_count)
{
	int infinite = (retry_count == -1);
	do
	{
		if(netlink_send_data(cmd_type, param, response)==0)
			return 0;
		cli_delay_ms(COMMAND_DELAY_MS);
	} while(infinite || retry_count-- > 0);
	return 1;
}

static char netlink_set_attribute_type(char cmd_type)
{
	char ret = 0;
	switch(cmd_type){
		case NL_SHELL_RUN:
		case NL_CLI_APP_GET_INFO:
			ret = NL_SHELL_RUN_CMD;
			break;
		case NL_SHELL_RUN_RAW:
			ret = NL_SHELL_RUN_CMD_RAW;
			break;
		case NL_CLI_APP_DRIVER:
			ret = NL_CLI_APP_DRIVER_CMD;
			break;
		default:
			ret = 0;
	}
	return ret;
}

int netlink_send_data(char cmd_type, char* param, char* response)
{
	char *pos;
	int nl_fd;  //netlink socket's file descriptor
	struct sockaddr_nl nl_address; //netlink socket address
	int nl_family_id = 0; //The family ID resolved by the netlink controller for this userspace program
	int nl_rxtx_length; //Number of bytes sent or received via send() or recv()
	struct nlattr *nl_na; //pointer to netlink attributes structure within the payload
	nl_msg_t nl_request_msg, nl_response_msg;
	int parse_data_length=0;
	char *argv[10] = { NULL, };
	int argc = 0;
	int len = 0;
	char tid;
	char string[16];

	if ((pos = strchr(param, '\n')) != NULL){
		*pos = '\0';
	}

	//Step 0: reset response data
	memset(response, 0x0, NL_MSG_MAX_RESPONSE_SIZE);

	//Step 1: Open the socket. Note that protocol = NETLINK_GENERIC
	nl_fd = socket(AF_NETLINK, SOCK_RAW, NETLINK_GENERIC);
	if (nl_fd < 0) {
		printf("Error socket() :%d\n",  nl_rxtx_length);
		return -1;
	}

	//Step 2: Bind the socket.
	memset(&nl_address, 0, sizeof(nl_address));
	nl_address.nl_family = AF_NETLINK;
	nl_address.nl_groups = 0;

	if (bind(nl_fd, (struct sockaddr *) &nl_address, sizeof(nl_address)) < 0) {
		printf("Error bind()\n");
		close(nl_fd);
		return -1;
	}

	//Step 3. Resolve the family ID corresponding to the string "NRC-NL-FAM"
	//Populate the netlink header
	nl_request_msg.n.nlmsg_type = GENL_ID_CTRL;
	nl_request_msg.n.nlmsg_flags = NLM_F_REQUEST;
	nl_request_msg.n.nlmsg_seq = 0;
	nl_request_msg.n.nlmsg_pid = getpid();
	nl_request_msg.n.nlmsg_len = NLMSG_LENGTH(GENL_HDRLEN);
	//Populate the payload's "family header" : which in our case is genlmsghdr
	nl_request_msg.g.cmd = CTRL_CMD_GETFAMILY;
	nl_request_msg.g.version = 0x1;
	//Populate the payload's "netlink attributes"
	nl_na = (struct nlattr *) GENLMSG_DATA(&nl_request_msg);
	nl_na->nla_type = CTRL_ATTR_FAMILY_NAME;
	nl_na->nla_len = strlen("NRC-NL-FAM") + 1 + NLA_HDRLEN;
	strcpy(NLA_DATA(nl_na), "NRC-NL-FAM"); //Family name length can be upto 16 chars including \0

	nl_request_msg.n.nlmsg_len += NLMSG_ALIGN(nl_na->nla_len);

	memset(&nl_address, 0, sizeof(nl_address));
	nl_address.nl_family = AF_NETLINK;

	//Send the family ID request message to the netlink controller
	nl_rxtx_length = sendto(nl_fd, (char *)&nl_request_msg, nl_request_msg.n.nlmsg_len,
						0, (struct sockaddr *) &nl_address, sizeof(nl_address));

	if (nl_rxtx_length != nl_request_msg.n.nlmsg_len) {
		printf("Error sendto() :%d\n",  nl_rxtx_length);
		close(nl_fd);
		return -1;
	}

	//Wait for the response message
	nl_rxtx_length = recv(nl_fd, &nl_response_msg, sizeof(nl_response_msg), 0);

	if (nl_rxtx_length < 0) {
		printf("Error recv() :%d\n",  nl_rxtx_length);
		return -1;
	}

	//Validate response message
	if (!NLMSG_OK((&nl_response_msg.n), nl_rxtx_length)) {
		fprintf(stderr, "nl_rxtx_length=%d\n", nl_rxtx_length);
		fprintf(stderr, "family ID request : invalid message\n");
		return -1;
	}

	if (nl_response_msg.n.nlmsg_type == NLMSG_ERROR) { //error
		fprintf(stderr, "family ID request : receive error\n");
		return -1;
	}

	//Extract family ID
	nl_na = (struct nlattr *) GENLMSG_DATA(&nl_response_msg);
	nl_na = (struct nlattr *) ((char *) nl_na + NLA_ALIGN(nl_na->nla_len));
	if (nl_na->nla_type == CTRL_ATTR_FAMILY_ID) {
		nl_family_id = *(__u16 *) NLA_DATA(nl_na);
	}

	//Step 4. Send own custom message
	memset(&nl_request_msg, 0, sizeof(nl_request_msg));
	memset(&nl_response_msg, 0, sizeof(nl_response_msg));

	nl_request_msg.n.nlmsg_len = NLMSG_LENGTH(GENL_HDRLEN);
	nl_request_msg.n.nlmsg_type = nl_family_id;
	nl_request_msg.n.nlmsg_flags = NLM_F_REQUEST;
	nl_request_msg.n.nlmsg_seq = 60;
	nl_request_msg.n.nlmsg_pid = getpid();
	nl_request_msg.g.cmd = cmd_type;

	nl_na = (struct nlattr *) GENLMSG_DATA(&nl_request_msg);

	if(cmd_type == NL_SHELL_RUN  || cmd_type == NL_SHELL_RUN_RAW
		|| cmd_type == NL_CLI_APP_GET_INFO || cmd_type == NL_CLI_APP_DRIVER){
		nl_na->nla_type = netlink_set_attribute_type(cmd_type);
		len = strlen(param)+ 1; // including '\0'
		nl_na->nla_len =  len + NLA_HDRLEN; //Message length
		memcpy(NLA_DATA(nl_na), param, len);
		nl_request_msg.n.nlmsg_len += NLMSG_ALIGN(nl_na->nla_len);
	}
	else if(cmd_type == NL_WFA_CAPI_SEND_ADDBA || cmd_type == NL_WFA_CAPI_SEND_DELBA){
		argc = util_cmd_parse_line(param, argv);
		memset(string, 0x0, sizeof(string));
		nl_na->nla_type = NL_WFA_CAPI_PARAM_TID;
		memcpy(string, argv[0], 1);
		tid = hex_to_int(string[0]);
		nl_na->nla_len =  NL_TID_BYTE_SIZE  +  NLA_HDRLEN;
		memcpy(NLA_DATA(nl_na), &tid,  NL_TID_BYTE_SIZE);
		nl_request_msg.n.nlmsg_len += NLMSG_ALIGN(nl_na->nla_len);
		if(argc == 2){
			nl_na = (struct nlattr *)((char*)nl_na + NLMSG_ALIGN(nl_na->nla_len));
			nl_na->nla_type = NL_WFA_CAPI_PARAM_DESTADDR;
			memset(string, 0x0, sizeof(string));
			macaddr_to_ascii(argv[1], string);
			nl_na->nla_len =  NL_MAC_ADDRESS_BYTE_SIZE + NLA_HDRLEN;
			memcpy(NLA_DATA(nl_na), string, NL_MAC_ADDRESS_BYTE_SIZE);
			nl_request_msg.n.nlmsg_len += NLMSG_ALIGN(nl_na->nla_len);
		}
	}
	memset(&nl_address, 0, sizeof(nl_address));
	nl_address.nl_family = AF_NETLINK;

	//Send the custom message
	nl_rxtx_length = sendto(nl_fd, (char *) &nl_request_msg, nl_request_msg.n.nlmsg_len,
						0, (struct sockaddr *) &nl_address, sizeof(nl_address));

	if (nl_rxtx_length != nl_request_msg.n.nlmsg_len) {
		printf("Error sendto() :%d\n",  nl_rxtx_length);
		close(nl_fd);
		return -1;
	}

	if(cmd_type == NL_SHELL_RUN  || cmd_type == NL_SHELL_RUN_RAW
		|| cmd_type == NL_CLI_APP_GET_INFO || cmd_type == NL_CLI_APP_DRIVER){
		//Receive reply from kernel
		nl_rxtx_length = recv(nl_fd, &nl_response_msg, sizeof(nl_response_msg), 0);

		if (nl_rxtx_length < 0) {
			printf("Error recv() :%d\n",  nl_rxtx_length);
			return -1;
		}

		//Validate response message
		if (nl_response_msg.n.nlmsg_type == NLMSG_ERROR) { //Error
			printf("Error while receiving reply from kernel: NACK Received\n");
			close(nl_fd);
			return -1;
		}
		if (nl_rxtx_length < 0) {
			printf("Error while receiving reply from kernel\n");
			close(nl_fd);
			return -1;
		}
		if (!NLMSG_OK((&nl_response_msg.n), nl_rxtx_length)) {
			printf("Error while receiving reply from kernel: Invalid Message\n");
			close(nl_fd);
			return -1;
		}

		//Parse the reply message
		nl_rxtx_length = GENLMSG_PAYLOAD(&nl_response_msg.n);
		nl_na = (struct nlattr *) GENLMSG_DATA(&nl_response_msg);
		int str_len = 0;
		int copy_pos = 0;

		if(cmd_type == NL_SHELL_RUN_RAW)
		{
			if ((nl_na->nla_len - 4) > 0)
				memcpy(response, NLA_DATA(nl_na), nl_na->nla_len - 4);
		}
		else
		{
			while(1)
			{
				//printf("Kernel replied: %s %d\n",(char *)NLA_DATA(nl_na), strlen((char *)NLA_DATA(nl_na)));
				str_len =  strlen((char *)NLA_DATA(nl_na));
				strncpy((response+copy_pos),(char *)NLA_DATA(nl_na), str_len);
				parse_data_length += NLA_ALIGN(nl_na->nla_len);

				copy_pos += (str_len );
				if(parse_data_length < nl_rxtx_length){
					*(response+copy_pos)=',';
					copy_pos++;
					nl_na = (struct nlattr *) ((char *) nl_na + NLA_ALIGN(nl_na->nla_len));
				}
				else{
					break;
				}
			}
		}

	}

	//Step 5. Close the socket and quit
	close(nl_fd);
	return 0;
}

/*******************************************************************************
* Firmware log follower
*
* The firmware ships its console output to the host as HIF_TYPE_LOG frames,
* which the driver republishes on the "nrc-log" generic-netlink multicast
* group (nrc_netlink_rx()). Subscribing to that group is the only way to read
* the firmware log on a production board, where the console UART is not
* brought out.
*******************************************************************************/

#ifndef SOL_NETLINK
#define SOL_NETLINK 270
#endif

/* HIF_LOG_SUB_* as the firmware stamps them on each frame. */
static const char *hlog_severity(unsigned char sub)
{
	switch (sub) {
	case 0:  return "VB";
	case 1:  return "INFO";
	case 2:  return "ERR";
	case 3:  return "ASSERT";
	case 4:  return "NONE";
	default: return "?";
	}
}

/*
 * Resolve the family id and the id the kernel assigned to the "nrc-log"
 * multicast group. netlink_send_data() above only needs the family id, so
 * it stops before CTRL_ATTR_MCAST_GROUPS; we need both.
 */
static int hlog_resolve(int nl_fd, __u16 *family_id, __u32 *group_id)
{
	nl_msg_t req, resp;
	struct sockaddr_nl addr;
	struct nlattr *na;
	int len, remain;

	memset(&req, 0, sizeof(req));
	memset(&resp, 0, sizeof(resp));

	req.n.nlmsg_type = GENL_ID_CTRL;
	req.n.nlmsg_flags = NLM_F_REQUEST;
	req.n.nlmsg_seq = 0;
	req.n.nlmsg_pid = getpid();
	req.n.nlmsg_len = NLMSG_LENGTH(GENL_HDRLEN);
	req.g.cmd = CTRL_CMD_GETFAMILY;
	req.g.version = 1;

	na = (struct nlattr *) GENLMSG_DATA(&req);
	na->nla_type = CTRL_ATTR_FAMILY_NAME;
	na->nla_len = strlen("NRC-NL-FAM") + 1 + NLA_HDRLEN;
	strcpy(NLA_DATA(na), "NRC-NL-FAM");
	req.n.nlmsg_len += NLMSG_ALIGN(na->nla_len);

	memset(&addr, 0, sizeof(addr));
	addr.nl_family = AF_NETLINK;

	if (sendto(nl_fd, (char *) &req, req.n.nlmsg_len, 0,
			(struct sockaddr *) &addr, sizeof(addr)) < 0) {
		return -1;
	}

	len = recv(nl_fd, &resp, sizeof(resp), 0);
	if (len < 0 || !NLMSG_OK(&resp.n, (unsigned int) len) ||
			resp.n.nlmsg_type == NLMSG_ERROR) {
		return -1;
	}

	*family_id = 0;
	*group_id = 0;

	na = (struct nlattr *) GENLMSG_DATA(&resp);
	remain = GENLMSG_PAYLOAD(&resp.n);

	while (remain > (int) NLA_HDRLEN) {
		if (na->nla_len < NLA_HDRLEN || na->nla_len > remain)
			break;

		if (na->nla_type == CTRL_ATTR_FAMILY_ID) {
			*family_id = *(__u16 *) NLA_DATA(na);
		} else if (na->nla_type == CTRL_ATTR_MCAST_GROUPS) {
			/* nested: one anonymous entry per group, each holding
			 * a NAME and an ID attribute */
			struct nlattr *grp = (struct nlattr *) NLA_DATA(na);
			int grp_remain = na->nla_len - NLA_HDRLEN;

			while (grp_remain > (int) NLA_HDRLEN) {
				struct nlattr *f = (struct nlattr *) NLA_DATA(grp);
				int f_remain = grp->nla_len - NLA_HDRLEN;
				const char *name = NULL;
				__u32 id = 0;

				if (grp->nla_len < NLA_HDRLEN || grp->nla_len > grp_remain)
					break;

				while (f_remain > (int) NLA_HDRLEN) {
					if (f->nla_len < NLA_HDRLEN || f->nla_len > f_remain)
						break;
					if (f->nla_type == CTRL_ATTR_MCAST_GRP_NAME)
						name = (const char *) NLA_DATA(f);
					else if (f->nla_type == CTRL_ATTR_MCAST_GRP_ID)
						id = *(__u32 *) NLA_DATA(f);
					f_remain -= NLA_ALIGN(f->nla_len);
					f = (struct nlattr *) ((char *) f + NLA_ALIGN(f->nla_len));
				}

				if (name && strcmp(name, "nrc-log") == 0)
					*group_id = id;

				grp_remain -= NLA_ALIGN(grp->nla_len);
				grp = (struct nlattr *) ((char *) grp + NLA_ALIGN(grp->nla_len));
			}
		}

		remain -= NLA_ALIGN(na->nla_len);
		na = (struct nlattr *) ((char *) na + NLA_ALIGN(na->nla_len));
	}

	return (*family_id && *group_id) ? 0 : -1;
}

/* Open a socket already joined to the nrc-log group. */
static int hlog_open(__u16 *family_id, __u32 *group_id)
{
	struct sockaddr_nl addr;
	int nl_fd;

	nl_fd = socket(AF_NETLINK, SOCK_RAW, NETLINK_GENERIC);
	if (nl_fd < 0)
		return -1;

	memset(&addr, 0, sizeof(addr));
	addr.nl_family = AF_NETLINK;
	if (bind(nl_fd, (struct sockaddr *) &addr, sizeof(addr)) < 0) {
		close(nl_fd);
		return -1;
	}

	if (hlog_resolve(nl_fd, family_id, group_id) < 0) {
		close(nl_fd);
		return -1;
	}

	if (setsockopt(nl_fd, SOL_NETLINK, NETLINK_ADD_MEMBERSHIP,
			group_id, sizeof(*group_id)) < 0) {
		close(nl_fd);
		return -1;
	}

	return nl_fd;
}

int netlink_log_follow(void)
{
	int nl_fd = -1;
	__u16 family_id = 0;
	__u32 group_id = 0;
	int announced = 0;
	/*
	 * A frame is a batch of bytes, not of lines: the firmware fills each
	 * frame to its payload limit, so a log line can straddle a frame
	 * boundary. Hold the tail back until its newline arrives, otherwise
	 * long lines (a stack dump, say) come out chopped in two.
	 */
	static char pending[NL_MSG_MAX_RESPONSE_SIZE * 2];
	size_t pending_len = 0;

	setvbuf(stdout, NULL, _IOLBF, 0);

	for (;;) {
		char buf[NL_MSG_MAX_RESPONSE_SIZE * 2];
		struct nlmsghdr *nh;
		struct timeval tv;
		int len;

		/*
		 * Reconnect rather than exit. This runs as a service, and
		 * every `systemctl restart halow.target` rmmods the driver:
		 * the family disappears and the group id is reassigned on
		 * reload, so a socket joined to the old id goes quietly deaf
		 * - the same failure mode that made the gateway stop hearing
		 * the driver once before.
		 */
		if (nl_fd < 0) {
			nl_fd = hlog_open(&family_id, &group_id);
			if (nl_fd < 0) {
				if (!announced) {
					fprintf(stderr, "hlog: waiting for the "
						"nrc driver (nrc-log group)\n");
					announced = 1;
				}
				sleep(2);
				continue;
			}
			fprintf(stderr, "hlog: following nrc-log "
				"(family %u, group %u)\n",
				(unsigned) family_id, (unsigned) group_id);
			announced = 0;
			pending_len = 0;
		}

		/* Wake up periodically even when idle, so a driver reload is
		 * noticed instead of blocking in recv() forever. */
		tv.tv_sec = 20;
		tv.tv_usec = 0;
		setsockopt(nl_fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

		len = recv(nl_fd, buf, sizeof(buf), 0);
		if (len < 0) {
			__u16 f = 0;
			__u32 g = 0;
			int probe;

			if (errno == EINTR)
				continue;
			if (errno != EAGAIN && errno != EWOULDBLOCK) {
				fprintf(stderr, "hlog: recv(): %s\n", strerror(errno));
				close(nl_fd);
				nl_fd = -1;
				continue;
			}

			/* Idle timeout: re-resolve on a throwaway socket (so a
			 * queued log message is not consumed by the reply) and
			 * rejoin if the driver came back as a different id. */
			probe = socket(AF_NETLINK, SOCK_RAW, NETLINK_GENERIC);
			if (probe >= 0) {
				struct sockaddr_nl paddr;

				memset(&paddr, 0, sizeof(paddr));
				paddr.nl_family = AF_NETLINK;
				if (bind(probe, (struct sockaddr *) &paddr,
						sizeof(paddr)) == 0) {
					if (hlog_resolve(probe, &f, &g) < 0 ||
							f != family_id || g != group_id) {
						close(nl_fd);
						nl_fd = -1;
					} else if (setsockopt(nl_fd, SOL_NETLINK,
							NETLINK_ADD_MEMBERSHIP,
							&group_id, sizeof(group_id)) < 0) {
						/*
						 * Same ids as before is not proof the
						 * membership survived: an rmmod clears
						 * every socket's group bit, and a reload
						 * can be handed the same family id and
						 * the same (first free) group id, leaving
						 * this socket deaf with nothing to notice.
						 * Re-adding is idempotent, so do it every
						 * idle period; if even that fails, rejoin.
						 */
						close(nl_fd);
						nl_fd = -1;
					}
				}
				close(probe);
			}
			continue;
		}

		for (nh = (struct nlmsghdr *) buf; NLMSG_OK(nh, (unsigned int) len);
				nh = NLMSG_NEXT(nh, len)) {
			struct genlmsghdr *gh;
			struct nlattr *na;
			int remain;
			const char *msg = NULL;
			unsigned char sub = 255;
			char *nl;

			if (nh->nlmsg_type != family_id)
				continue;

			gh = (struct genlmsghdr *) NLMSG_DATA(nh);
			if (gh->cmd != NL_CMD_LOG_EVENT)
				continue;

			na = (struct nlattr *) GENLMSG_DATA(nh);
			remain = GENLMSG_PAYLOAD(nh);

			while (remain > (int) NLA_HDRLEN) {
				if (na->nla_len < NLA_HDRLEN || na->nla_len > remain)
					break;
				if (na->nla_type == NL_CMD_LOG_MSG)
					msg = (const char *) NLA_DATA(na);
				else if (na->nla_type == NL_CMD_LOG_TYPE)
					sub = *(unsigned char *) NLA_DATA(na);
				remain -= NLA_ALIGN(na->nla_len);
				na = (struct nlattr *) ((char *) na + NLA_ALIGN(na->nla_len));
			}

			if (!msg)
				continue;

			if (pending_len + strlen(msg) < sizeof(pending)) {
				strcpy(pending + pending_len, msg);
				pending_len += strlen(msg);
			} else {
				/* A line this long is not a line; drop it and resync.
				 * Say so in the stream - every other hole in this log
				 * is marked, and a silent one would be the exception
				 * a reader could not know about. */
				printf("[FW/%s] [hlog-host] %zu bytes discarded: no line "
					"end within the buffer\n", hlog_severity(sub),
					pending_len + strlen(msg));
				pending_len = 0;
				continue;
			}

			while ((nl = memchr(pending, '\n', pending_len)) != NULL) {
				*nl = '\0';
				printf("[FW/%s] %s\n", hlog_severity(sub), pending);
				/* an assert batch is the last thing the firmware
				 * will ever say; do not let it sit in a buffer */
				if (sub == 3)
					fflush(stdout);
				pending_len -= (nl + 1 - pending);
				memmove(pending, nl + 1, pending_len);
			}
		}
	}

	close(nl_fd);
	return 0;
}

#endif /* _CLI_NETLINK_ */
