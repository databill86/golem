import json
import logging
import time

import importlib
import os
import re
from django.conf import settings

from golem.core import message_logger
from golem.core.chat_session import ChatSession
from golem.core.responses.responses import TextMessage
from golem.tasks import accept_inactivity_callback, accept_schedule_callback
from .context import Context
from .flow import Flow, load_flows_from_definitions
from .logger import Logger
from .persistence import get_redis
from .serialize import json_deserialize, json_serialize
from .tests import ConversationTestRecorder


class DialogManager:
    version = '1.32'

    def __init__(self, session: ChatSession):
        self.session = session
        self.uid = session.chat_id  # for backwards compatibility
        self.logger = Logger(session)
        self.profile = None  # session.interface.load_profile(session.interface)  # FIXME not here
        self.db = get_redis()
        self.log = logging.getLogger()
        self.context = None  # type: Context

        self.should_log_messages = settings.GOLEM_CONFIG.get('SHOULD_LOG_MESSAGES', False)
        self.error_message_text = settings.GOLEM_CONFIG.get('ERROR_MESSAGE_TEXT', 'Oh no! You broke me! :(')

        context_dict = {}
        version = self.db.get('dialog_version')
        self.log.info('Initializing dialog for chat %s...' % session.chat_id)
        self.current_state_name = None
        self.init_flows()

        if version and version.decode('utf-8') == DialogManager.version and \
                self.db.hexists('session_context', self.session.chat_id):

            state = self.db.hget('session_state', self.session.chat_id).decode('utf-8')
            self.log.info('Session exists at state %s' % state)
            self.move_to(state, initializing=True)
            # self.log.info(entities_string)
            context_string = self.db.hget('session_context', self.session.chat_id)
            context_dict = json.loads(context_string.decode('utf-8'), object_hook=json_deserialize)
        else:
            self.current_state_name = 'default.root'
            self.log.info('Creating new session...')
            self.logger.log_user(self.profile)

        self.context = Context.from_dict(dialog=self, data=context_dict)

    def init_flows(self):
        flow_definitions = self.create_flows()
        self.flows = load_flows_from_definitions(flow_definitions)
        self.current_state_name = 'default.root'

    def create_flows(self):
        import yaml
        flows = {}
        BOTS = settings.GOLEM_CONFIG.get('BOTS', [])
        for filename in BOTS:
            try:
                with open(os.path.join(settings.BASE_DIR, filename)) as f:
                    flows.update(yaml.load(f))
            except OSError as e:
                raise ValueError("Unable to open definition {}".format(filename)) from e
        return flows

    @staticmethod
    def clear_chat(chat_id):
        db = get_redis()
        db.hdel('session_state', chat_id)
        db.hdel('session_context', chat_id)

    def move_by_entities(self, entities):
        self.move_to('default.root')
        # TODO instead of this, first check for states in this flow that accept the entity
        # TODO then check for states in default flow OR check for flows that accept it

    def process(self, message_type, entities):
        self.session.interface.processing_start(self.session)
        accepted_time = time.time()
        accepted_state = self.current_state_name
        # Only process messages and postbacks (not 'seen_by's, etc)
        if message_type not in ['message', 'postback', 'schedule']:
            return

        self.log.info('-- USER message ----------------------------------')

        # if message_type != 'schedule':
        self.context.counter += 1

        entities = self.context.add_entities(entities)

        if self.test_record_message(message_type, entities):
            return

        if message_type != 'schedule':
            self.save_inactivity_callback()

        self.log.info('++ PROCESSING ++++++++++++++++++++++++++++++++++++')

        if not self.check_state_transition():
            if not self.check_intent_transition():
                print("FOO", entities.keys())  # FIXME
                if self.get_state().accepts_message(entities.keys()):
                    self.run_accept(save_identical=True)
                    self.save_state()
                else:
                    self.move_by_entities(entities)
                    self.save_state()

        self.session.interface.processing_end(self.session)

        # leave logging message to the end so that the user does not wait
        self.logger.log_user_message(message_type, entities, accepted_time, accepted_state)

    def schedule(self, callback_name, at=None, seconds=None):
        self.log.info('Scheduling callback "{}": at {} / seconds: {}'.format(callback_name, at, seconds))
        if at:
            if at.tzinfo is None or at.tzinfo.utcoffset(at) is None:
                raise Exception('Use datetime with timezone, e.g. "from django.utils import timezone"')
            accept_schedule_callback.apply_async((self.session.to_json(), callback_name), eta=at)
        elif seconds:
            accept_schedule_callback.apply_async((self.session.to_json(), callback_name), countdown=seconds)
        else:
            raise Exception('Specify either "at" or "seconds" parameter')

    def inactive(self, callback_name, seconds):
        self.log.info('Setting inactivity callback "{}" after {} seconds'.format(callback_name, seconds))
        accept_inactivity_callback.apply_async(
            (self.session.to_json(), self.context.counter, callback_name, seconds),
            countdown=seconds)

    def save_inactivity_callback(self):
        self.db.hset('session_active', self.session.chat_id, time.time())
        callbacks = settings.GOLEM_CONFIG.get('INACTIVE_CALLBACKS')
        if not callbacks:
            return
        for name in callbacks:
            seconds = callbacks[name]
            self.inactive(name, seconds)

    def test_record_message(self, message_type, entities):
        record, record_age = self.context.get_age('test_record')
        self.recording = False
        if not record:
            return False
        if record_age == 0:
            if record.value == 'start':
                self.send_response(ConversationTestRecorder.record_start())
            elif record.value == 'stop':
                self.send_response(ConversationTestRecorder.record_stop())
            else:
                self.send_response("Use /test_record/start/ or /test_record/stop/")
            self.save_state()
            return True
        if record == 'start':
            ConversationTestRecorder.record_user_message(message_type, entities)
            self.recording = True
        return False

    def run_accept(self, save_identical=False):
        self.log.info('Running action of state {}'.format(self.current_state_name))
        state = self.get_state()
        if not state.action:
            self.log.warning('State does not have an action.')
            return
        state.action(dialog=self)
        # self.send_response(response)
        # self.move_to(new_state_name, save_identical=save_identical)

    def run_init(self):
        self.run_accept()
        # self.log.warning('Running INIT action of {}'.format(self.current_state_name))
        # state = self.get_state()
        # if not state.init:
        #     self.log.warning('State does not have an INIT action, we are done.')
        #     return
        # response, new_state_name = state.init(state=state)
        # self.send_response(response)
        # self.move_to(new_state_name)

    def check_state_transition(self):
        new_state_name = self.context.get('_state', max_age=0)
        return self.move_to(new_state_name)

    def check_intent_transition(self):
        intent = self.context.intent.get()
        if not intent:
            return False
        # FIXME Get custom intent transition
        new_state_name = None # self.get_state().get_intent_transition(intent)
        # If no custom intent transition present, move to the flow whose 'intent' field matches intent
        # Check accepted intent of the current flow's states
        if not new_state_name:
            flow = self.get_flow()
            new_state_name = flow.get_state_for_intent(intent)

        # Check accepted intent of all flows
        if not new_state_name:
            for flow in self.flows.values():
                if flow.matches_intent(intent):
                    new_state_name = flow.name + '.root'
                    break

        if not new_state_name:
            self.log.error('Error! Found intent "%s" but no flow present for it!' % intent)
            return False

        # new_state_name = new_state_name + ':accept'
        self.log.info('Moving based on intent %s...' % intent)
        return self.move_to(new_state_name)

    def get_flow(self, flow_name=None):
        if not flow_name:
            flow_name, _ = self.current_state_name.split('.', 1)
        return self.flows.get(flow_name)

    def get_state(self, flow_state_name=None):
        flow_name, state_name = (flow_state_name or self.current_state_name).split('.', 1)
        flow = self.get_flow(flow_name)
        return flow.get_state(state_name) if flow else None

    def move_to(self, new_state_name, initializing=False, save_identical=False):
        # if flow prefix is not present, add the current one
        action = 'init'
        if isinstance(new_state_name, int):
            new_state = self.context.get_history_state(new_state_name - 1)
            new_state_name = new_state['name'] if new_state else None
            action = None
        if not new_state_name:
            new_state_name = self.current_state_name
        if new_state_name.count(':'):
            new_state_name, action = new_state_name.split(':', 1)
        if ('.' not in new_state_name):
            new_state_name = self.current_state_name.split('.')[0] + '.' + new_state_name
        if not self.get_state(new_state_name):
            self.log.info('Error: State %s does not exist! Staying at %s.' % (new_state_name, self.current_state_name))
            return False
        identical = new_state_name == self.current_state_name
        if not initializing and (not identical or save_identical):
            self.context.add_state(new_state_name)
        if not new_state_name or identical:
            return False
        previous_state = self.current_state_name
        self.current_state_name = new_state_name
        if not initializing:
            self.log.info('MOVING %s -> %s %s' % (previous_state, new_state_name, action))

            # notify the interface that the state was changed
            self.session.interface.state_change(self.current_state_name)
            # record change if recording tests
            if self.recording:
                ConversationTestRecorder.record_state_change(self.current_state_name)

            try:
                logging.error(previous_state)
                logging.error(new_state_name)
                if previous_state != new_state_name:
                    new_state = self.get_state(new_state_name)
                    if new_state.check_requirements():
                        self.run_accept()
                    else:
                        requirement = new_state.get_first_requirement()
                        requirement.action(self)

            except Exception as e:
                logging.error('*****************************************************')
                logging.error('Exception occurred while running action {} of state {}'
                              .format(action, new_state_name))
                logging.error('Chat id: {}'.format(self.session.chat_id))
                try:
                    context_debug = self.get_state().dialog.context.debug()
                    logging.error('Context: {}'.format(context_debug))
                except:
                    pass
                logging.exception('Exception follows')
                self.send_response([TextMessage(self.error_message_text)])

        self.save_state()
        return True

    def save_state(self):
        if not self.context:
            return
        self.log.info('Saving state at %s' % (self.current_state_name))
        self.db.hset('session_state', self.session.chat_id, self.current_state_name)
        context_json = json.dumps(self.context.to_dict(), default=json_serialize)
        self.db.hset('session_context', self.session.chat_id, context_json)
        self.db.hset('session_interface', self.session.chat_id, self.session.interface.name)
        self.db.set('dialog_version', DialogManager.version)

        # save chat session to redis, TODO
        session = json.dumps(self.session.to_json())
        self.db.hset("chat_session", self.session.chat_id, session)

    def send_response(self, responses, next=None):
        if not responses:
            return
        self.log.info('-- CHATBOT message -------------------------------')

        if not (isinstance(responses, list) or isinstance(responses, tuple)):
            return self.send_response([responses], next)

        for response in responses:
            if isinstance(response, str):
                response = TextMessage(text=response)

            # Send the response
            self.session.interface.post_message(self.session, response)

            # Record if recording
            if self.recording:
                ConversationTestRecorder.record_bot_message(response)

        for response in responses:
            # Log the response
            self.log.info('Message: {}'.format(response))
            self.logger.log_bot_message(response, self.current_state_name)

            text = response.text if hasattr(response, 'text') else (response if isinstance(response, str) else None)
            if text and self.should_log_messages:
                message_logger.on_message.delay(self.session, text, self, from_user=False)

        if next is not None:
            self.move_to(next)
