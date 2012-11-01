
import uuid
import random
import logging

log = logging.getLogger('coweb.session')

class late_join_handler:

    def __init__(self, session):

        self._session = session
        self._container = session._container

        # last state received from an updater without another sync message
        # intervening
        self._lastState = None

        # dictionary of updaters available. key is the clientId 
        # value is a list tokens the updater is updating
        self._updaters = {}

        # dictionary of clients currently waiting for state. key is update token
        # value is a BayeuxSession object associated with that token
        self._updatees = {}

        # list that helps us assign the lowest available site id
        self._siteids = ['reserved'] + [None] * 5

    def clear_last_state(self):
        '''Clears the last updater state response. No longer valid.'''
        self._lastState = None

    def get_updater_count(self):
        '''Gets the number of updaters.'''
        return len(self._updaters)

    def get_site_for_client(self, client):
        '''Gets the site ID associated with a client.'''
        return self._siteids.index(client.clientId)

    def add_site_for_client(self, client):
        '''Associates a client with a site ID.'''
        try:
            # find a vacant siteid
            siteId = self._siteids.index(None)
        except ValueError:
            siteId = len(self._siteids)
            # expand available siteids
            self._siteids.append(None)
        self._siteids[siteId] = client.clientId
        # never changes, so stash it on the client for fast lookup
        client.siteId = siteId
        return siteId

    def remove_site_for_client(self, client):
        '''Disassociates site ID from client.'''
        try:
            clientId = self._siteids[client.siteId]
        except (KeyError, AttributeError):
            return
        # make sure we didn't get fouled up
        assert(clientId == client.clientId)
        # delete it
        self._siteids[client.siteId] = None
        return client.siteId

    def add_updater(self, client, notify=True):
        '''Sets a client as an updater.'''
        clientId = client.clientId
        # check if this client is already an updater and ignore unless this is
        # the first updater
        if clientId in self._updaters and len(self._updaters) > 0:
            return
        # add updater to the dict of available updaters
        self._updaters[clientId] = []
        if notify:
            # notify others of updater availability
            self._send_roster_available_message(client)

    def ensure_updater(self, client):
        '''Ensures a client is an updater. Exception if not.'''
        # just try to access to ensure
        self._updaters[client.clientId]

    def remove_updater(self, client):
        '''Removes a client as an updater.'''
        # remove this client from the assigned siteids
        self.remove_site_for_client(client)
        try:
            # remove this client from updaters
            tokenList = self._updaters[client.clientId]
            del self._updaters[client.clientId]
        except KeyError:
            # client wasn't an updater, but might be a joiner getting updated
            for token, updatee in self._updatees.items():
                if updatee.clientId == client.clientId:
                    # remove updatee from list
                    del self._updatees[token]
        else:
            # notify other attendees in the session of the leaving updater
            self._send_roster_unavailable_message(client)
            if len(tokenList):
                # client was updating joiners, send new updater to joiners
                for token in tokenList:
                    try:
                        updatee = self._updatees[token]
                        del self._updatees[token]
                    except KeyError:
                        continue
                    self.assign_updater(updatee)

    def assign_updater(self, updatee):
        '''Assigns an updater to a late-joiner.'''
        if not len(self._updaters):
            # no updaters left, this is now the only updater
            #self._updaters[updatee.clientId] = []
            self.add_updater(updatee, False)
            updatee.add_message({
                'channel':'/service/session/join/state',
                'data': []
            })
            return
        updaterId = None
        if updatee.updaterType is not 'default':
            matchedType = self._container.updaterTypeMatcher.match(updatee.updaterType, self.get_available_updater_types())
            if matchedType is not None:
                for clientId in self._updaters:
                    updater = self._session.get_client(clientId)
                    if updater.updaterType == matchedType:
                        updaterId = clientId
                        log.info('found an updater type of %s', matchedType)
                        break
        if updaterId is None:
            # grab random updater
            updaterId = random.choice(self._updaters.keys())
        updater = self._session.get_client(updaterId)
        # generate a unique token
        token = uuid.uuid4().hex
        # make sure we note who the updater is now updating
        self._updaters[updaterId].append(token)
        # make sure we note who the updatee expects the update from
        self._updatees[token] = updatee
        # now send the request
        updater.add_message({
            'channel':'/service/session/updater',
            'data': token
        })

    def get_available_updater_types(self):
        updaterTypes = [];
        for clientId in self._updaters:
            updater = self._session.get_client(clientId)
            updaterTypes.append(updater.updaterType)
        return updaterTypes

    def _get_roster_list(self, client):
        '''Builds the roster of all updaters.'''
        roster = {}
        # roster is all updater site IDs mapped to usernames
        for clientId in self._updaters:
            updater = self._session.get_client(clientId)
            roster[updater.siteId] = updater.username
        return roster

    def _send_roster_available_message(self, client):
        '''Sends a roster addition to all clients.'''
        msg = {
            'channel':'/session/roster/available',
            'data': {
                'siteId':client.siteId,
                'username':client.username
            }
        }
        self._session.publish(msg)

    def _send_roster_unavailable_message(self, client):
        '''Sends a roster removal to all clients.'''
        msg = {
            'channel':'/session/roster/unavailable',
            'data': {
                'username':client.username,
                'siteId':client.siteId
            }
        }
        self._session.publish(msg)

    def queue_updatee(self, client):
        '''Queues a late-joiner to receive full state.'''
        clientId = client.clientId

        try:
            # get site id already assigned
            siteid = self.get_site_for_client(client)
        except ValueError:
            # get new site id
            siteid = self.add_site_for_client(client)

        #log.debug('onJoinSession attendee = %s', str(attendee))

        # get the roster list to return before adding anyone
        roster = self._get_roster_list(client)

        # first client in
        if not len(self._updaters):
            #log.debug('first one in %s', str(attendee))
            # add client to updaters
            self.add_updater(client, False)
            #self._updaters[clientId] = []
            data = []
            sendState = True
        elif self._lastState is None:
            self.assign_updater(client)
            sendState = False
        else:
            data = self._lastState
            sendState = True

        # send site id to client
        client.add_message({
            'channel':'/service/session/join/siteid',
            'data': siteid
        })
        # send roster to client
        client.add_message({
            'channel':'/service/session/join/roster',
            'data': roster
        })
        # send empty state to first joiner or last state if available
        if sendState:
            client.add_message({
                'channel':'/service/session/join/state',
                'data': data
            })

    def unqueue_updatee(self, updater, data):
        '''Sends full state to a late-joiner waiting for it.'''
        clientId = updater.clientId
        token = None

        #log.debug('onStateResponse data: %s', str(data))
        #log.debug('clientId = %s', clientId)

        # let exception bubble if data is missing token
        token = data['token']
        # let exception bubble if this is not really an updater
        tokens = self._updaters[clientId]
        # let exception bubble if this updater does not hold this token
        self._updaters[clientId].remove(token)
        # return silently if the updatee no longer exists
        try:
            updatee = self._updatees[token]
            del self._updatees[token]
        except KeyError:
            return

        # store this state as the last known up-to-date state
        if self._session.cacheState is True:
            log.info('using cached state')
            self._lastState = data['state']

        # send state to updatee
        updatee.add_message({
            'channel': '/service/session/join/state',
            'data': data['state']
        })

