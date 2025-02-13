#!/usr/bin/env python3

from dateutil.parser import parse as parse_time
import numpy as np
import sys
import time
import pytz
import pprint
import secrets
import datetime
import gettext
import requests
import smtplib, ssl
from email.utils import make_msgid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import Header
from email.charset import Charset, QP
from flask_babel import get_locale as flask_get_locale

from sqlalchemy import create_engine, func, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import Column, Integer, String, Sequence, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import sessionmaker,relationship,column_property
from sqlalchemy.sql import case

from config import *

SQLITE_URI = 'sqlite:///foo.db'
NODE_OFFLINE_TIMEOUT = datetime.timedelta(hours=1)
DB_VERSION = 3

Base = declarative_base()

if not APP_URL.endswith('/'):
    APP_URL += '/'


class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, Sequence('user_id_seq'), primary_key=True)
    email = Column(String(50), unique=True)
    email_confirmed = Column(Boolean, default=False)
    email_token = Column(String(64), default=lambda: secrets.token_urlsafe(64))
    created_at = Column(DateTime, default=func.now())
    subscriptions = relationship("Subscription", back_populates="user")

    def __repr__(self):
        return "<User(email='%s', confirmed='%s')>" % (
                                self.email, str(self.email_confirmed))

    @classmethod
    def find_by_email(cls, session, email):
        return session.query(User).filter(User.email == email).one_or_none()

    def try_confirm(self, session, token):
        tokens_match = secrets.compare_digest(self.email_token, token)

        if not self.email_confirmed and tokens_match:
            self.email_confirmed = True
            session.add(self)
            session.commit()

        return tokens_match

    def get_mail_template(self, name):
        flask_locale = flask_get_locale()
        if flask_locale:
            language = flask_locale.language
        else:
            language = 'en'

        t = gettext.translation('messages', localedir='translations', languages=[language])

        if name == 'confirm':
            return {"subject": t.gettext("mail:confirm:subject"),
                    "message": t.gettext("mail:confirm:message")}
        elif name == 'alarm':
            return {"subject": t.gettext("mail:alarm:subject"),
                    "message": t.gettext("mail:alarm:message")}
        elif name == 'resolved':
            return {"subject": t.gettext("mail:resolved:subject"),
                    "message": t.gettext("mail:resolved:message")}
        else:
            raise Exception(f'No mail template named {name} found!')

    def send_confirm_mail(self, url):
        url = url + "?email=" + self.email + "&token=" + self.email_token
        mail_template = self.get_mail_template("confirm")

        self.send_mail(mail_template, url=url)

    def send_mail(self, mail_template, in_reply_to = None, **kwargs):
        msgid = make_msgid()

        subject = mail_template['subject'].format(**kwargs)
        message = mail_template['message'].format(**kwargs)

        msg = MIMEMultipart('alternative')
        msg['Subject'] = str(Header(subject, 'utf-8'))
        msg['From'] = str(Header(SMTP_FROM, 'utf-8'))
        msg['To'] = str(Header(self.email, 'utf-8'))
        msg['Message-ID'] = msgid
        msg['Reply-To'] = SMTP_REPLY_TO_EMAIL
        msg['Date'] = datetime.datetime.now(pytz.utc).strftime("%a, %e %b %Y %T %z")

        if in_reply_to:
            msg['In-Reply-To'] = in_reply_to
            msg['References'] = in_reply_to

        # add message
        charset = Charset('utf-8')
        # QP = quoted printable; this is better readable instead of base64, when
        # the mail is read in plaintext!
        charset.body_encoding = QP
        message_part = MIMEText(message.encode('utf-8'), 'plain', charset)
        msg.attach(message_part)

        if DEBUG:
            with open("/tmp/keepitup_mails.log", "a") as f:
                f.write(msg.as_string() + "\n")
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.ehlo()
                if SMTP_USE_STARTTLS:
                    context = ssl.create_default_context()
                    server.starttls(context=context)

                server.sendmail(SMTP_FROM, self.email, msg.as_string())

        return msgid

    @property
    def subscribed_nodes(self):
        return [subscription.node for subscription in self.subscriptions]


class NodesJSONCache:

    def __init__(self):
        self.nodes = []

    def update(self, nodeset=None):
        res = requests.get(NODES_JSON_URL)

        if not res.ok:
            print("warning: NodesJSONCache could not download " + NODES_JSON_URL + "!", file=sys.stderr)
            return

        nodes = []
        try:
            json = res.json()
            for node in json['nodes']:
                nodeinfo = node['nodeinfo']

                if nodeset:
                    db_node = nodeset.find_by_nodeid(nodeinfo['node_id'])
                    if db_node:
                        nodes += [db_node]
                        continue

                n = Node(nodeinfo['hostname'], nodeinfo['node_id'])
                if 'lastseen' in node:
                    n.last_seen_at = parse_time(node['lastseen'])
                n.last_updated_at = parse_time(json['timestamp'])
                nodes += [n]
        except KeyError:
            print("warning: NodesJSONCache detected wrong format for " + NODES_JSON_URL + "!", file=sys.stderr)
            return

        self.nodes = nodes

    def find_by_nodeid(self, nodeid):
        for node in self.nodes:
            if node.nodeid == nodeid:
                return node

    def update_db_node(self, node):
        """ Update a node from the nodes.json. This makes only sense, if
        nodes_json_cache.update() has been called with nodeset=None.
        Otherwise the nodes_json_cache contains the nodes from nodeset. """

        other = self.find_by_nodeid(node.nodeid)

        # node does not exist in nodes.json anymore, so we can not
        # update it.
        if not other:
            return

        node.name = other.name
        node.last_seen_at = other.last_seen_at
        node.last_updated_at = other.last_updated_at


class Subscription(Base):
    __tablename__ = 'subscriptions'

    user_id = Column(Integer, ForeignKey('users.id'), primary_key=True, nullable=False)
    node_id = Column(Integer, ForeignKey('nodes.id'), primary_key=True, nullable=False)
    send_notifications = Column(Boolean, default=True)
    user = relationship("User", back_populates="subscriptions")
    node = relationship("Node", back_populates="subscriptions")


class NodeSet:

    def __init__(self):
        self.nodes = []

    def update_from_db(self, session, filter_user=None):
        # force reload from db
        session.expire_all()

        q = session.query(Node)
        if filter_user is not None:
            # TODO: update here
            q = q.filter(Node.user == filter_user)

        self.nodes = q.all()

    def find_by_nodeid(self, nodeid):
        for node in self.nodes:
            if node.nodeid == nodeid:
                return node


class Alarm(Base):
    __tablename__ = 'alarms'

    id = Column(Integer, Sequence('alarm_id_seq'), primary_key=True)
    node_id = Column(Integer, ForeignKey('nodes.id'))
    node = relationship("Node", back_populates="alarms")
    alarm_at = Column(DateTime, default=func.now())
    alarm_mail_msgid = Column(String(995), default=None)
    resolved_at = Column(DateTime, default=None)
    is_resolved = column_property(case(
        [(resolved_at == None, False)], else_=True))
    state = column_property(case(value=is_resolved, whens={
        True: 'ok',
        False: 'alarm'
    }))

    @property
    def duration_str(self):
        if not self.is_resolved:
            return "ongoing"

        delta = self.resolved_at - self.alarm_at

        if delta.total_seconds() > 24*60*60:
            return "%d days" % (delta.total_seconds() / 24 / 60 / 60)

        if delta.total_seconds() > 60*60:
            return "%d hrs" % (delta.total_seconds() / 60 / 60)

        if delta.total_seconds() > 60:
            return "%d min" % (delta.total_seconds() / 60)

        return "%d s" % delta.total_seconds()

    def send_notification_mails(self, session):
        node = self.node
        url = APP_URL + 'node/' + self.node.nodeid

        for subscription in node.subscriptions:
            if not subscription.send_notifications:
                continue

            user = subscription.user

            if self.is_resolved:
                mail_template = user.get_mail_template("resolved")
                user.send_mail(mail_template, in_reply_to=self.alarm_mail_msgid, node=node, url=url)
            else:
                mail_template = user.get_mail_template("alarm")
                self.alarm_mail_msgid = user.send_mail(mail_template, node=node, url=url)

                session.add(self)
                session.commit()


class Node(Base):
    __tablename__ = 'nodes'

    id = Column(Integer, Sequence('node_id_seq'), primary_key=True)
    name = Column(String(64))
    nodeid = Column(String(32), unique=True)
    state = Column(String(16))
    # The state 'unknown' is stored in the separate column is_state_unknown, so
    # we can recover the old state as soon as is_state_unknown becomes False
    # again.
    is_state_unknown = Column(Boolean, default=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    subscriptions = relationship("Subscription", back_populates="node")
    alarms = relationship("Alarm", back_populates="node", order_by="desc(Alarm.id)")
    last_seen_at = Column(DateTime)
    last_updated_at = Column(DateTime)

    def __init__(self, name, nodeid):
        self.name = name
        self.nodeid = nodeid
        self.state = "new"
        self.is_state_unknown = True

    def _switch_state(self, session):
        old_state = self.state

        if datetime.datetime.now() - self.last_seen_at > NODE_OFFLINE_TIMEOUT:
            new_state = 'problem'
        else:
            new_state = 'ok'

        if new_state != old_state:
            self.state = new_state
            session.add(self)
            session.commit()

        return old_state, new_state

    def _update_waiting(self, session):
        """ This function calculates and updates the waiting status."""

        self.is_state_unknown = ( \
            self.last_updated_at is None \
            or self.last_seen_at is None \
            or datetime.datetime.now() - self.last_updated_at > datetime.timedelta(minutes=5) \
        )
        session.add(self)
        session.commit()

    def check(self, session):
        """ Checks for state changes. Returns an Alarm object, if an alarm
        has just been created, or an alarm has just been resolved. This means
        it only returns an Alarm object if a state change happened. """

        self._update_waiting(session)
        if self.is_state_unknown:
            # switching state is only allowed when node is not waiting
            return None

        old_state, new_state = self._switch_state(session)
        alarm = None

        if old_state == 'ok' and new_state == 'problem':
            alarm = Alarm(node=self)
            alarm.alarm_at = self.last_seen_at

        if old_state == 'new' and new_state == 'problem':
            alarm = Alarm(node=self)
            alarm.alarm_at = datetime.datetime.now()

        if old_state == 'problem' and new_state == 'ok':
            alarm = self.latest_alarm(session)
            alarm.resolved_at = self.last_seen_at

        if alarm:
            session.add(alarm)
            session.commit()
            alarm.send_notification_mails(session)

        return alarm

    def latest_alarm(self, session):
        return session.query(Alarm).\
            filter(Alarm.node_id == self.id).\
            order_by(Alarm.id.desc()).\
            limit(1).one_or_none()

    @property
    def subscribed_users(self):
        return [subscription.user for subscription in self.subscriptions]

    @property
    def is_in_db(self):
        # nodes in db have an id, others don't
        return bool(self.id)

    def get_subscription_by_user(self, session, user):
        if user is None:
            return None
        return session.query(Subscription).\
            filter(Subscription.node == self).\
            filter(Subscription.user == user).\
            one_or_none()

    @property
    def constitution(self):
        if self.is_state_unknown:
            return 'unknown'
        else:
            return self.state


class DBVersion(Base):
    __tablename__ = 'db_version'

    id = Column(Integer, Sequence('version_id_seq'), primary_key=True)
    version = Column(Integer)

    def __init__(self):
        self.id = 1

    @classmethod
    def get(cls, session):
        version_obj = session.query(DBVersion).one_or_none()
        if not version_obj:
            return 0
        return version_obj.version

    @classmethod
    def set(cls, session, new_version):
        version_obj = session.query(DBVersion).one_or_none()
        if not version_obj:
            version_obj = DBVersion()
        version_obj.version = new_version
        session.add(version_obj)


def get_session():
    engine = create_engine(SQLITE_URI)

    Session = sessionmaker()
    Session.configure(bind=engine)
    return Session()


def init_db():
    engine = create_engine(SQLITE_URI)

    classes = [Node, User, Alarm, Subscription]

    for cls in classes:
        cls.metadata.create_all(engine)

    db = get_session()
    DBVersion.set(db, DB_VERSION)
    db.commit()


if __name__ == '__main__':
    db = get_session()

    nodeset = NodeSet()
    nodeset.update_from_db(db)
