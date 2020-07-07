import asynchat
import asyncore
import logging
import socket
import sys
from utility import *
import commands
from datetime import datetime, timedelta
from threading import Thread

PY3 = sys.version_info[0] == 3
if PY3:
    from queue import Queue

logger = logging.getLogger(name="tmi")


class TwitchChat(object):

    def __init__(self, user, admin, oauth, channels):
        self.logger = logging.getLogger(name="twitch_chat")
        self.channels = channels
        self.admin = admin
        self.user = user
        self.oauth = oauth
        self.channel_servers = {'irc.chat.twitch.tv:6667': {'channel_set': channels}}
        self.irc_handlers = []
        self.admins = commands.ADMIN
        self.commands = commands.COMMAND
        self.notice = commands.NOTICE
        self.returns = commands.RETURNS
        self.state = self.load_state()
        self.limiter = MessageLimiter()
        self.active = True
        self.command_thread = Thread(target=self.handle_commandline_input)
        self.command_thread.daemon = True
        self.command_thread.start()
        self.backup_thread = Thread(target=self.backup_thread)
        self.backup_thread.daemon = True
        self.backup_thread.start()
        for server in self.channel_servers:
            handler = IrcClient(server, self.handle_message, self.handle_connect, self.can_send_type)
            self.channel_servers[server]['client'] = handler
            self.irc_handlers.append(handler)

    def can_send_type(self, msg_type: MessageType):
        return convert(self.state.get(msg_type.name, "True"))

    def save_state(self):
        loadToText(self.state, "texts/global_state.txt")

    @staticmethod
    def load_state():
        return loadToDictionary("texts/global_state.txt")

    def start(self):
        for handler in self.irc_handlers:
            handler.start()

    def join(self):
        for handler in self.irc_handlers:
            handler.asynloop_thread.join()

    def stop_all(self):
        for handler in self.irc_handlers:
            handler.stop()

    def check_error(self, irc_message):
        """Check for a login error notification and terminate if found"""
        if re.search(r":tmi.twitch.tv NOTICE \* :Error logging i.*", irc_message):
            self.logger.critical(
                "Error logging in to twitch irc, check your oauth and username are set correctly in config.txt!")
            self.stop_all()
            return True

    def check_join(self, irc_message):
        """Watch for successful channel join messages"""
        match = re.search(r':{0}!{0}@{0}\.tmi\.twitch\.tv JOIN #(.*)'.format(self.user), irc_message)
        if match:
            if match.group(1) in self.channels:
                self.logger.info("Joined channel {0} successfully".format(match.group(1)))
                return True

    def check_part(self, irc_message):
        """Watch for successful channel join messages"""
        match = re.search(r':{0}!{0}@{0}\.tmi\.twitch\.tv PART #(.*)'.format(self.user), irc_message)
        if match:
            self.logger.info("Left channel {0} successfully".format(match.group(1)))
            return True

    def check_usernotice(self, irc_message):
        """Parse out new twitch subscriber messages and then call... python subscribers"""
        if irc_message[0] == '@':
            arg_regx = r"([^=;]*)=([^ ;]*)"
            arg_regx = re.compile(arg_regx, re.UNICODE)
            args = dict(re.findall(arg_regx, irc_message[1:]))
            regex = (
                r'^@[^ ]* :tmi.twitch.tv'
                r' USERNOTICE #(?P<channel>[^ ]*)'  # channel
                r'((?: :)?(?P<message>.*))?')  # message
            regex = re.compile(regex, re.UNICODE)
            match = re.search(regex, irc_message)
            if match:
                args['channel'] = match.group(1)
                args['message'] = match.group(2)
                for func_name, func in self.notice.items():
                    func(self, args)
                return True

    @staticmethod
    def check_ping(irc_message, client):
        """Respond to ping messages or twitch boots us off"""
        if re.search(r"PING :tmi\.twitch\.tv", irc_message):
            message = Message("PING :pong\r\n", MessageType.FUNCTIONAL)
            client.send_message(message)
            return True

    def check_message(self, irc_message):
        """Watch for chat messages and notifiy subsribers"""
        if irc_message[0] == "@":
            arg_regx = r"([^=;]*)=([^ ;]*)"
            arg_regx = re.compile(arg_regx, re.UNICODE)
            args = dict(re.findall(arg_regx, irc_message[1:]))
            regex = (r'^@[^ ]* :([^!]*)![^!]*@[^.]*.tmi.twitch.tv'  # username
                     r' PRIVMSG #([^ ]*)'  # channel
                     r' :(.*)')  # message
            regex = re.compile(regex, re.UNICODE)
            match = re.search(regex, irc_message)
            if match:
                args['username'] = match.group(1)
                args['channel'] = match.group(2)
                args['message'] = match.group(3)
                self.logger.debug(args["message"])
                if args["username"] == self.admin:
                    for func_name, func in self.admins.items():
                        func(self, args)
                for func_name, func in self.returns.items():
                    if func(self, args):
                        return True
                for func_name, func in self.commands.items():
                    func(self, args)
                return True

    def handle_connect(self, client):
        self.logger.info('Connected..authenticating as {0}'.format(self.user))
        client.send_message(Message('Pass ' + self.oauth + '\r\n', MessageType.FUNCTIONAL))
        client.send_message(Message('NICK ' + self.user + '\r\n'.lower(), MessageType.FUNCTIONAL))
        client.send_message(Message('CAP REQ :twitch.tv/tags\r\n', MessageType.FUNCTIONAL))
        client.send_message(Message('CAP REQ :twitch.tv/membership\r\n', MessageType.FUNCTIONAL))
        client.send_message(Message('CAP REQ :twitch.tv/commands\r\n', MessageType.FUNCTIONAL))

        for server in self.channel_servers:
            if server == client.serverstring:
                self.logger.info('Joining channels {0}'.format(self.channel_servers[server]))
                for chan in self.channel_servers[server]['channel_set']:
                    client.send_message(Message('JOIN ' + '#' + chan.lower() + '\r\n', MessageType.FUNCTIONAL))

    def join_twitch_channel(self, channel: str):
        self.logger.info('Joining channel {0}'.format(channel))
        channels = self.channel_servers.get('irc.chat.twitch.tv:6667').get("channel_set")
        channels.append(channel)
        self.channel_servers['irc.chat.twitch.tv:6667']['channel_set'] = channels
        self.channels = channels
        client = self.channel_servers["irc.chat.twitch.tv:6667"]["client"]
        client.send_message(Message("JOIN #" + channel.lower() + "\r\n", MessageType.FUNCTIONAL))

    def leave_twitch_channel(self, channel: str):
        self.logger.info('Leaving channel {0}'.format(channel))
        client = self.channel_servers["irc.chat.twitch.tv:6667"]["client"]
        client.send_message(Message("PART #" + channel.lower() + "\r\n", MessageType.FUNCTIONAL))
        channels = self.channel_servers.get('irc.chat.twitch.tv:6667').get("channel_set")
        updated_channels = [chan for chan in channels if chan != channel]
        self.channel_servers['irc.chat.twitch.tv:6667']['channel_set'] = updated_channels
        self.channels = updated_channels

    def handle_message(self, irc_message, client):
        """Handle incoming IRC messages"""
        self.logger.debug(irc_message)
        if self.check_message(irc_message):
            return
        elif self.check_join(irc_message):
            return
        elif self.check_part(irc_message):
            return
        elif self.check_usernotice(irc_message):
            return
        elif self.check_ping(irc_message, client):
            return
        elif self.check_error(irc_message):
            return

    def send_message(self, channel: str, message: Message):
        for server in self.channel_servers:
            if channel in self.channel_servers[server]['channel_set']:
                client = self.channel_servers[server]['client']
                client.send_message(Message(u'PRIVMSG #{0} :{1}\n'.format(channel, message.content), message.type))
                break

    def backup_thread(self):
        while self.active:
            time.sleep(600)
            self.save_state()
            commands.save_db()
            self.logger.info("Backup thread go BRRRRRRRR")

    def handle_commandline_input(self):
        while self.active:
            ans = input()
            match = re.match(r'send (.*)', ans)
            if ans == "save":
                self.save_state()
                commands.save_db()
            elif ans == "stop":
                self.active = False
                self.save_state()
                self.stop_all()
            elif ans == "join":
                print("Which channel?")
                channel = input()
                self.join_twitch_channel(channel)
            elif ans == "leave":
                print("Which channel?")
                channel = input()
                if channel not in self.channels:
                    print("Not following {0}".format(channel))
                else:
                    self.leave_twitch_channel(channel)
            elif ans == "reload":
                commands.reload()
            elif ans == "toggle spam":
                self.state[MessageType.SPAM.name] = str(not convert(self.state.get(MessageType.SPAM.name, "True")))
            elif ans == "toggle command":
                self.state[MessageType.COMMAND.name] = str(
                    not convert(self.state.get(MessageType.COMMAND.name, "True")))
            elif ans == "toggle bld":
                self.state[MessageType.BLACKLISTED.name] = str(
                    not convert(self.state.get(MessageType.BLACKLISTED.name, "True")))
            elif ans == "toggle helpful":
                self.state[MessageType.HELPFUL.name] = str(
                    not convert(self.state.get(MessageType.HELPFUL.name, "True")))
            elif ans == "toggle special":
                self.state[MessageType.SPECIAL.name] = str(
                    not convert(self.state.get(MessageType.SPECIAL.name, "True")))
            elif ans == "toggle sub":
                self.state[MessageType.SUBSCRIBER.name] = str(
                    not convert(self.state.get(MessageType.SUBSCRIBER.name, "True")))
            elif ans == "toggle all":
                for key in self.state:
                    val = self.state.get(key)
                    if val == "False" or val == "True":
                        self.state[key] = str(not convert(val))
            elif ans == "state":
                print(self.state)
            elif ans == "db":
                print(commands.temp_db)
            elif match:
                msg = Message(match.group(1), MessageType.CHAT)
                print("channel?")
                ans = input()
                if ans in self.channels:
                    self.send_message(ans, msg)
            else:
                print("save\nstop\njoin\nleave\nreload\nstate\ndb\nsend (msg)\ntoggle (type)")


MAX_SEND_RATE = 20
SEND_RATE_WITHIN_SECONDS = 30


class IrcClient(asynchat.async_chat, object):

    def __init__(self, server, message_callback, connect_callback, allowed_callback):
        self.logger = logging.getLogger(name="tmi_client[{0}]".format(server))
        self.logger.info('TMI initializing')
        self.map = {}
        asynchat.async_chat.__init__(self, map=self.map)
        self.received_data = bytearray()
        servernport = server.split(":")
        self.serverstring = server
        self.server = servernport[0]
        self.port = int(servernport[1])
        self.set_terminator(b'\n')
        self.asynloop_thread = Thread(target=self.run)
        self.running = False
        self.message_callback = message_callback
        self.connect_callback = connect_callback
        self.allowed_callback = allowed_callback
        self.message_queue = Queue()
        self.messages_sent = []
        self.logger.info('TMI initialized')
        return

    def send_message(self, msg: Message):
        self.message_queue.put(msg)

    def handle_connect(self):
        """Socket connected successfully"""
        self.connect_callback(self)

    def handle_error(self):
        if self.socket:
            self.close()
        raise

    def collect_incoming_data(self, data):
        """Dump recieved data into a buffer"""
        self.received_data += data

    def found_terminator(self):
        """Processes each line of text received from the IRC server."""
        txt = self.received_data.rstrip(b'\r')  # accept RFC-compliant and non-RFC-compliant lines.
        del self.received_data[:]
        self.message_callback(txt.decode("utf-8"), self)

    def start(self):
        """Connect start message watching thread"""
        if not self.asynloop_thread.is_alive():
            self.running = True
            self.asynloop_thread = Thread(target=self.run)
            self.asynloop_thread.daemon = True
            self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
            self.connect((self.server, self.port))
            self.asynloop_thread.start()

            self.send_thread = Thread(target=self.send_loop)
            self.send_thread.daemon = True
            self.send_thread.start()

        else:
            self.logger.critical("Already running can't run twice")

    def stop(self):
        """Terminate the message watching thread by killing the socket"""
        self.running = False
        if self.asynloop_thread.is_alive():
            if self.socket:
                self.close()
            try:
                self.asynloop_thread.join()
                self.send_thread.join()
            except RuntimeError as e:
                if str(e) == "cannot join current thread":
                    # this is thrown when joining the current thread and is ok.. for now"
                    pass
                else:
                    raise e

    def send_loop(self):
        while self.running:
            try:
                if len(self.messages_sent) < MAX_SEND_RATE:
                    if not self.message_queue.empty():
                        to_send = self.message_queue.get()
                        self.logger.info(str(to_send))
                        if self.allowed_callback(to_send.type):
                            self.push(to_send.content.encode("UTF-8"))
                            time.sleep(random.randint(50, 150) / 100)
                        self.messages_sent.append(datetime.now())
                else:
                    time_cutoff = datetime.now() - timedelta(seconds=SEND_RATE_WITHIN_SECONDS)
                    self.messages_sent = [dt for dt in self.messages_sent if dt < time_cutoff]
            except Exception:
                pass

    def run(self):
        """Loop!"""
        try:
            asyncore.loop(map=self.map)
        finally:
            self.running = False
