##
# Copyright (c) 2010 Sprymix Inc.
# All rights reserved.
#
# See LICENSE for details.
##


from semantix.caos import session


class SessionPool(session.SessionPool):
    def __init__(self, backend, realm):
        super().__init__(realm)
        self.backend = backend

    def create(self):
        return Session(self.realm, self.backend, pool=self)


class Session(session.Session):

    def __init__(self, realm, backend, pool):
        super().__init__(realm, entity_cache=session.WeakEntityCache, pool=pool)
        self.backend = backend
        self.xact = []
        self.connection = self.backend.connection_pool(self)

    def get_connection(self):
        return self.connection

    def _new_transaction(self):
        self.get_connection()
        xact = self.connection.xact()
        xact.begin()
        return xact

    def in_transaction(self):
        return super().in_transaction() and bool(self.xact)

    def begin(self):
        super().begin()
        self.xact.append(self._new_transaction())

    def commit(self):
        super().commit()
        xact = self.xact.pop()
        xact.commit()

    def rollback(self):
        super().rollback()
        if self.xact:
            xact = self.xact.pop()
            xact.rollback()

    def rollback_all(self):
        super().rollback_all()
        while self.xact:
            self.xact.pop().rollback()

    def _store_entity(self, entity):
        self.backend.store_entity(entity, self)

    def _delete_entities(self, entities):
        self.backend.delete_entities(entities, self)

    def _store_links(self, source, targets, link_name, merge=False):
        self.backend.store_links(source, targets, link_name, self, merge=merge)

    def _delete_links(self, source, targets, link_name):
        self.backend.delete_links(source, targets, link_name, self)

    def _load_link(self, link):
        return self.backend.load_link(link._instancedata.source, link._instancedata.target, link,
                                      self)

    def load(self, id, concept=None):
        if not concept:
            concept_name = self.backend.concept_name_from_id(id, session=self)
            concept = self.schema.get(concept_name)
        else:
            concept_name = concept._metadata.name

        links = self.backend.load_entity(concept_name, id, session=self)

        if not links:
            return None

        return self._load(id, concept, links)

    def sequence_next(self, seqcls):
        return self.backend.sequence_next(seqcls)

    def start_batch(self, batch):
        super().start_batch(batch)
        self.backend.start_batch(self, id(batch))

    def commit_batch(self, batch):
        super().commit_batch(batch)
        self.backend.commit_batch(self, id(batch))

    def close_batch(self, batch):
        super().close_batch(batch)
        self.backend.close_batch(self, id(batch))

    def _store_entity_batch(self, entities, batch):
        self.backend.store_entity_batch(entities, self, id(batch))

    def _store_link_batch(self, links, batch):
        self.backend.store_link_batch(links, self, id(batch))

    def sync(self):
        self.do_sync()

    def release(self):
        super().release()
        self.connection.reset()

    def close(self):
        super().close()
        self.connection.release()
        self.connection = None
