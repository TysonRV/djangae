import threading

from functools import partial
from itertools import chain, groupby

from djangae.db.backends.appengine import caching
from djangae.db import utils

from djangae.db.backends.appengine import POLYMODEL_CLASS_ATTRIBUTE


class AsyncMultiQuery(object):
    """
        Runs multiple queries simultaneously and merges the result sets based on the
        shared ordering.
    """
    THREAD_COUNT = 2

    def __init__(self, queries, orderings):
        self._queries = queries
        self._orderings = orderings
        self._min_max_cache = {}

    def _spawn_thread(self, i, query, result_queues, **query_run_args):
        class Thread(threading.Thread):
            def __init__(self, *args, **kwargs):
                self.results_fetched = False
                super(Thread, self).__init__(*args, **kwargs)

            def run(self):
                result_queues[i] = (x for x in query.Run(**query_run_args))
                self.results_fetched = True

        thread = Thread()
        thread.start()
        return thread

    def _fetch_results(self):
        threads = []

        # We need to grab a set of results per query
        result_queues = [None] * len(self._queries)

        # Go through the queries, trigger new threads as they become available
        for i, query in enumerate(self._queries):
            while len(threads) >= self.THREAD_COUNT:
                try:
                    complete = (x for x in threads if x.is_finished).next()
                except StopIteration:
                    # No threads available, continue waiting
                    continue

                # Remove the complete thread
                complete.join()
                threads.remove(complete)

            # Spawn a new thread
            threads.append(self._spawn_thread(i, query, result_queues))

        [x.join() for x in threads] # Wait until all the threads are done

        return result_queues

    def _compare_entities(self, lhs, rhs):
        def get_extreme_if_list_property(entity_key, column, value, order):
            if not isinstance(value, list):
                return value

            if (entity_key, column) in self._min_max_cache:
                return self._min_max_cache[(entity_key, column)]

            if order == Query.DESCENDING:
                value = min(value)
            else:
                value = max(value)
            self._min_max_cache[(entity_key, column)] = value

        if not lhs:
            return cmp(lhs, rhs)

        for column, order in self._orderings:
            lhs_value = lhs.key() if column == "__key__" else lhs[column]
            rhs_value = rhs.key() if column == "__key__" else rhs[column]

            lhs_value = get_extreme_if_list_property(lhs.key(), column, lhs_value, order)
            rhs_value = get_extreme_if_list_property(lhs.key(), column, rhs_value, order)

            result = cmp(lhs_value, rhs_value)

            if order == Query.DESCENDING:
                result = -result
            if result:
                return result

        return cmp(lhs.key(), rhs.key())


    def Run(self, **kwargs):
        self._min_max_cache = []
        results = self._fetch_results()

        next_entries = [None] * len(results)
        for i, queue in enumerate(results):
            try:
                next_entries[i] = results[i].next()
            except StopIteration:
                next_entries[i] = None

        seen_keys = set() #For de-duping results
        while any(next_entries):
            next = sorted(next_entries, self._compare_entities)[0]

            i = next_entries.index(next)
            try:
                next_entries[i] = results[i].next()
            except StopIteration:
                next_entries[i] = None

            # Make sure we haven't seen this result before before yielding
            if next.key() not in seen_keys:
                seen_keys.add(next.key())
                yield next


def _convert_entity_based_on_query_options(entity, opts):
    if opts.keys_only:
        return entity.key()

    if opts.projection:
        for k in entity.keys()[:]:
            if k not in list(opts.projection) + [POLYMODEL_CLASS_ATTRIBUTE]:
                del entity[k]

    return entity


class QueryByKeys(object):
    """ Does the most efficient fetching possible for when we have the keys of the entities we want. """

    def __init__(self, connection, model, queries, ordering, namespace):
        self.connection = connection

        # `queries` should be filtered by __key__ with keys that have the namespace applied to them.
        # `namespace` is passed for explicit niceness (mostly so that we don't have to assume that
        # all the keys belong to the same namespace, even though they will).
        def _get_key(query):
            result = query["__key__ ="]
            return result

        self.model = model
        self.namespace = namespace

        # groupby requires that the iterable is sorted by the given key before grouping
        self.queries = sorted(queries, key=_get_key)
        self.queries_by_key = { a: list(b) for a, b in groupby(self.queries, _get_key) }

        self.ordering = ordering
        self._Query__kind = queries[0]._Query__kind

    def Run(self, limit=None, offset=None):
        """
            Here are the options:

            1. Single key, hit memcache
            2. Multikey projection, async MultiQueries with ancestors chained
            3. Full select, datastore get
        """

        opts = self.queries[0]._Query__query_options
        key_count = len(self.queries_by_key)

        is_projection = False

        results = None
        if key_count == 1:
            # FIXME: Potentially could use get_multi in memcache and the make a query
            # for whatever remains
            key = self.queries_by_key.keys()[0]
            result = caching.get_from_cache_by_key(key)
            if result is not None:
                results = [result]
                cache = False # Don't update cache, we just got it from there

        if results is None:
            if opts.projection:
                is_projection = True # Don't cache projection results!

                # Assumes projection ancestor queries are faster than a datastore Get
                # due to lower traffic over the RPC. This should be faster for queries with
                # < 30 keys (which is the most common case), and faster if the entities are
                # larger and there are many results, but there is probably a slower middle ground
                # because the larger number of RPC calls. Still, if performance is an issue the
                # user can just do a normal get() rather than values/values_list/only/defer

                to_fetch = (offset or 0) + limit if limit else None
                additional_cols = set([ x[0] for x in self.ordering if x[0] not in opts.projection])

                multi_query = []
                final_queries = []
                orderings = self.queries[0]._Query__orderings
                for key, queries in self.queries_by_key.iteritems():
                    for query in queries:
                        if additional_cols:
                            # We need to include additional orderings in the projection so that we can
                            # sort them in memory. Annoyingly that means reinstantiating the queries
                            query = self.connection.query(
                                kind=query._Query__kind,
                                filters=query,
                                projection=list(opts.projection).extend(list(additional_cols)),
                                namespace=self.namespace,
                            )

                        query.Ancestor(key) # Make this an ancestor query
                        multi_query.append(query)
                        if len(multi_query) == 30:
                            final_queries.append(AsyncMultiQuery(multi_query, orderings).Run(limit=to_fetch))
                            multi_query = []
                else:
                    if len(multi_query) == 1:
                        final_queries.append(multi_query[0].Run(limit=to_fetch))
                    elif multi_query:
                        final_queries.append(AsyncMultiQuery(multi_query, orderings).Run(limit=to_fetch))

                results = chain(*final_queries)
            else:
                results = self.connection.get_multi(self.queries_by_key.keys())

        def iter_results(results):
            returned = 0
            # This is safe, because Django is fetching all results any way :(
            sorted_results = sorted(results, cmp=partial(utils.django_ordering_comparison, self.ordering))
            sorted_results = [result for result in sorted_results if result is not None]
            if not is_projection and sorted_results:
                caching.add_entities_to_cache(
                    self.model,
                    sorted_results,
                    caching.CachingSituation.DATASTORE_GET,
                    self.namespace,
                )

            for result in sorted_results:
                if is_projection:
                    entity_matches_query = True
                else:
                    entity_matches_query = any(
                        utils.entity_matches_query(result, qry) for qry in self.queries_by_key[result.key()]
                    )

                if not entity_matches_query:
                    continue

                if offset and returned < offset:
                    # Skip entities based on offset
                    returned += 1
                    continue
                else:

                    yield _convert_entity_based_on_query_options(result, opts)

                    returned += 1

                    # If there is a limit, we might be done!
                    if limit is not None and returned == (offset or 0) + limit:
                        break

        return iter_results(results)

    def Count(self, limit, offset):
        return len([x for x in self.Run(limit, offset)])


class NoOpQuery(object):
    def Run(self, limit, offset):
        return []

    def Count(self, limit, offset):
        return 0


class UniqueQuery(object):
    """
        This mimics a normal query but hits the cache if possible. It must
        be passed the set of unique fields that form a unique constraint
    """
    def __init__(self, connection, unique_identifier, gae_query, model, namespace):
        self.connection = connection
        self._identifier = unique_identifier
        self._gae_query = gae_query
        self._model = model
        self._namespace = namespace

        self._Query__kind = gae_query._Query__kind

    def get(self, x):
        return self._gae_query.get(x)

    def keys(self):
        return self._gae_query.keys()

    def Run(self, limit, offset):
        opts = self._gae_query._Query__query_options
        if opts.keys_only or opts.projection:
            return self._gae_query.Run(limit=limit, offset=offset)

        ret = caching.get_from_cache(self._identifier, self._namespace)
        if ret is not None and not utils.entity_matches_query(ret, self._gae_query):
            ret = None

        if ret is None:
            # We do a fast keys_only query to get the result
            keys_query = self.connection.query(
                kind=self._gae_query._Query__kind,
                keys_only=True,
                namespace=self._namespace
            )
            keys_query.update(self._gae_query)
            keys = keys_query.Run(limit=limit, offset=offset)

            # Do a consistent get so we don't cache stale data, and recheck the result matches the query
            ret = [x for x in self.connection.get_multi(keys) if x and utils.entity_matches_query(x, self._gae_query)]
            if len(ret) == 1:
                caching.add_entities_to_cache(
                    self._model,
                    [ret[0]],
                    caching.CachingSituation.DATASTORE_GET,
                    self._namespace,
                )
            return iter(ret)

        return iter([ret])

    def Count(self, limit, offset):
        return sum(1 for x in self.Run(limit, offset))
