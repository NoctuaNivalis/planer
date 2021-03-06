
import asyncio
import json
import datetime
import re
import traceback

from pony.orm import db_session, select
from pony.orm.serialization import json_converter
from simpledate import SimpleDate

from planer.daemon.db import db
from planer.config import config

__all__ = ["run_api_server"]


@asyncio.coroutine
def run_api_server():
    server_closing = asyncio.Future()
    coro = asyncio.start_server(ConnectionHandler(server_closing),
                                config['daemon']['host'],
                                config['daemon']['port'])
    server = yield from coro

    # Serve requests until Ctrl+C is pressed
    print('Serving on {}'.format(server.sockets[0].getsockname()))
    message = yield from server_closing
    print(message)

    # Close the server
    server.close()
    yield from server.wait_closed()


class HandlerException(RuntimeError):
    pass


class ConnectionHandler(object):

    HANDLERS = {}

    def __init__(self, close_server):
        self.close_server = close_server
        self.handlers = self.__class__.HANDLERS
        self.handlers['exit'] = self.exit

    @asyncio.coroutine
    def __call__(self, reader, writer):

        data = yield from reader.readline()
        message = json.loads(data.decode())
        addr = writer.get_extra_info('peername')
        print("Received {} from {}".format(repr(message), addr))

        try:
            if "action" not in message:
                raise HandlerException("Please provide an action in your json.")
            action = message.pop("action", "no action")
            if action not in self.handlers:
                raise HandlerException(
                        "'{}' is not a valid action.".format(action))
            handler = self.handlers[action]
            answer = handler(message) or {}
            answer["success"] = True
        except HandlerException as e:
            traceback.print_exc()
            answer = dict(error=str(e), success=False)
        json_answer = json.dumps(answer, default=json_converter)
        writer.write("{}\n".format(json_answer).encode())
        yield from writer.drain()

        print("Close the client socket")
        writer.close()

    def exit(self, message):
        self.close_server.set_result("Closing server on request.")

    @classmethod
    def add_handler_for(cls, action):
        def handler_adder(handler):
            cls.HANDLERS[action] = handler
            return handler
        return handler_adder


@db_session
def get_entity(id, table, word):
    if not id:
        raise HandlerException("Please provide {}".format(word))
    with db_session: entity = table.get(id=id)
    if not entity:
        raise HandlerException(
                "{} with that id not found.".format(table._table_))
    return entity


@ConnectionHandler.add_handler_for("echo")
def echo(message):
    message["action"] = "echo"
    return message


@ConnectionHandler.add_handler_for("help")
def help(_):
    return dict(commands=list(ConnectionHandler.HANDLERS.keys()))


@ConnectionHandler.add_handler_for("list")
def list_(message):
    if "calendar" in message:
        calendar = get_entity(message.get("calendar", None),
                              db.Calendar,
                              "a calendar")
        with db_session:
            events = list(select(e.id for e in db.Event if e.calendar == calendar))
        reply = dict(ids=events)
    else:
        with db_session:
            calendars = list(select(c.id for c in db.Calendar))
        reply = dict(ids=calendars)
    return reply


@ConnectionHandler.add_handler_for("show")
@db_session # TODO remove this when somehow to_dict no longer
            # retrieves the calendar
def show(message):
    if "event" in message: key, cls, word = "event", db.Event, "an event"
    elif "calendar" in message: key, cls = "calendar", db.Calendar, "a calendar"
    else: raise HandlerException("Provide an event or calendar id to show.")
    entity = get_entity(message.get(key, None), cls, word)
    return entity.to_dict()


DURATION_REGEX = re.compile("(?P<hours>\d*):(?P<minutes>\d*):(?P<seconds>\d*)")
def parse_duration(duration):
    match = DURATION_REGEX.fullmatch(duration)
    if match is None:
        raise HandlerException("incorrect duration format")
    groups = match.groupdict()
    return datetime.timedelta(
            hours=int(groups.get("hours") or "0"),
            minutes=int(groups.get("minutes") or "0"),
            seconds=int(groups.get("seconds") or "0"))


@ConnectionHandler.add_handler_for("create-event")
def create_event(message):
    if ("end_time" in message) + ("duration" in message) != 1:
        raise HandlerException("Creating an event requires either a "
                + "end_time or a duration, not neither nor both.")
    try:
        calendar = get_entity(message.get("calendar", None),
                              db.Calendar,
                              "a calendar")
        event = dict(calendar=calendar.id,
                     summary=message["summary"],
                     description=message.get("description", None) or "",
                     location=message.get("location", None) or "")
        timezone = message.get('timezone', config['remote']['timezone'])
        event["start_time"] = SimpleDate(message["start_time"], tz=timezone).datetime
        if "duration" in message:
            event["end_time"] = event["start_time"]
            event["end_time"] += parse_duration(message["duration"])
        else:
            event["end_time"] = SimpleDate(message["end_time"], tz=timezone).datetime
        with db_session:
            e = db.Event(**event)
        return dict(id=e.id)
    except KeyError as exc:
        raise HandlerException("An event requires key: {}.".format(exc))
    except Exception as exc:
        raise HandlerException(exc)


