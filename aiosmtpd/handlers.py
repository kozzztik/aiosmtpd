"""Handlers which provide custom processing at various events.

At certain times in the SMTP protocol, various events can be processed.  These
events include the SMTP commands, and at the completion of the data receipt.
Pass in an instance of one of these classes, or derive your own, to provide
your own handling of messages.  Implement only the methods you care about.
"""

import sys
import asyncio
import logging
import mailbox
import smtplib

from .base_handler import BaseHandler
from email import message_from_bytes, message_from_string
from email.feedparser import NLCRE
from public import public


EMPTYSTRING = ''
COMMASPACE = ', '
CRLF = '\r\n'
log = logging.getLogger('mail.debug')


def _format_peer(peer):
    # This is a separate function mostly so the test suite can craft a
    # reproducible output.
    return 'X-Peer: {!r}'.format(peer)


@public
class Debugging(BaseHandler):
    def __init__(self, stream=None):
        super(Debugging, self).__init__()
        self.stream = sys.stdout if stream is None else stream

    @classmethod
    def from_cli(cls, parser, *args):
        error = False
        stream = None
        if len(args) == 0:
            pass
        elif len(args) > 1:
            error = True
        elif args[0] == 'stdout':
            stream = sys.stdout
        elif args[0] == 'stderr':
            stream = sys.stderr
        else:
            error = True
        if error:
            parser.error('Debugging usage: [stdout|stderr]')
        return cls(stream)

    def _print_message_content(self, peer, data):
        in_headers = True
        for line in data.splitlines():
            # Dump the RFC 2822 headers first.
            if in_headers and not line:
                print(_format_peer(peer), file=self.stream)
                in_headers = False
            if isinstance(data, bytes):
                # Avoid spurious 'str on bytes instance' warning.
                line = line.decode('utf-8', 'replace')
            print(line, file=self.stream)

    @asyncio.coroutine
    def handle_DATA(self, session, envelope):
        print('---------- MESSAGE FOLLOWS ----------', file=self.stream)
        # Yes, actually test for truthiness since it's possible for either the
        # keywords to be missing, or for their values to be empty lists.
        add_separator = False
        options = envelope.mail_from.options
        if options:
            print('mail options:', options, file=self.stream)
            add_separator = True
        # rcpt_options are not currently support by the SMTP class.
        for rcpt in envelope.rcpt_tos:
            if rcpt.options:
                print('rcpt options:', rcpt.options, file=self.stream)
                add_separator = True
        if add_separator:
            print(file=self.stream)
        self._print_message_content(session.peer, envelope.content)
        print('------------ END MESSAGE ------------', file=self.stream)


@public
class Proxy(BaseHandler):
    def __init__(self, remote_hostname, remote_port):
        self._hostname = remote_hostname
        self._port = remote_port

    @asyncio.coroutine
    def handle_DATA(self, session, envelope):
        lines = envelope.content.splitlines(keepends=True)
        # Look for the last header
        i = 0
        ending = CRLF
        for line in lines:                          # pragma: nobranch
            if NLCRE.match(line):
                ending = line
                break
            i += 1
        lines.insert(i, 'X-Peer: %s%s' % (session.peer[0], ending))
        data = EMPTYSTRING.join(lines)
        rcpts = [rcpt.address for rcpt in envelope.rcpt_tos]

        refused = self._deliver(envelope.mail_from.address, rcpts, data)
        # TBD: what to do with refused addresses?
        log.info('we got some refusals: %s', refused)

    def _deliver(self, mail_from, rcpt_tos, data):
        refused = {}
        try:
            s = smtplib.SMTP()
            s.connect(self._hostname, self._port)
            try:
                refused = s.sendmail(mail_from, rcpt_tos, data)
            finally:
                s.quit()
        except smtplib.SMTPRecipientsRefused as e:
            log.info('got SMTPRecipientsRefused')
            refused = e.recipients
        except (OSError, smtplib.SMTPException) as e:
            log.exception('got', e.__class__)
            # All recipients were refused.  If the exception had an associated
            # error code, use it.  Otherwise, fake it with a non-triggering
            # exception code.
            errcode = getattr(e, 'smtp_code', -1)
            errmsg = getattr(e, 'smtp_error', 'ignore')
            for r in rcpt_tos:
                refused[r] = (errcode, errmsg)
        return refused


@public
class Sink(BaseHandler):
    pass


@public
class Message(BaseHandler):
    def __init__(self, message_class=None):
        self.message_class = message_class

    @asyncio.coroutine
    def handle_DATA(self, session, envelope):
        envelope = self.prepare_message(session, envelope)
        yield from self.handle_message(envelope)

    def prepare_message(self, session, envelope):
        # If the server was created with decode_data True, then data will be a
        # str, otherwise it will be bytes.
        data = envelope.content
        if isinstance(data, bytes):
            message = message_from_bytes(data, self.message_class)
        else:
            assert isinstance(data, str), (
              'Expected str or bytes, got {}'.format(type(data)))
            message = message_from_string(data, self.message_class)
        message['X-Peer'] = str(session.peer)
        message['X-MailFrom'] = envelope.mail_from.address
        message['X-RcptTo'] = COMMASPACE.join(
            [rcpt.address for rcpt in envelope.rcpttos])
        return message

    @asyncio.coroutine
    def handle_message(self, message):
        raise NotImplementedError                   # pragma: nocover


@public
class Mailbox(Message):
    def __init__(self, mail_dir, message_class=None):
        self.mailbox = mailbox.Maildir(mail_dir)
        self.mail_dir = mail_dir
        super().__init__(message_class)

    @asyncio.coroutine
    def handle_message(self, message):
        self.mailbox.add(message)

    def reset(self):
        self.mailbox.clear()

    @classmethod
    def from_cli(cls, parser, *args):
        if len(args) < 1:
            parser.error('The directory for the maildir is required')
        elif len(args) > 1:
            parser.error('Too many arguments for Mailbox handler')
        return cls(args[0])
