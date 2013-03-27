import logging
import pylons
from sqlalchemy.exc import ProgrammingError

import ckan.plugins as p
import ckanext.datastore.logic.action as action
import ckanext.datastore.logic.auth as auth
import ckanext.datastore.db as db
import ckan.logic as logic
import ckan.model as model

log = logging.getLogger(__name__)
_get_or_bust = logic.get_or_bust


class DatastoreException(Exception):
    pass


class DatastorePlugin(p.SingletonPlugin):
    p.implements(p.IConfigurable, inherit=True)
    p.implements(p.IActions)
    p.implements(p.IAuthFunctions)

    legacy_mode = False

    def configure(self, config):
        self.config = config
        # check for ckan.datastore.write_url and ckan.datastore.read_url
        if (not 'ckan.datastore.write_url' in config):
            error_msg = 'ckan.datastore.write_url not found in config'
            raise DatastoreException(error_msg)

        # Legacy mode means that we have no read url. Consequently sql search is not
        # available and permissions do not have to be changed. In legacy mode, the
        # datastore runs on PG prior to 9.0 (for example 8.4).
        self.legacy_mode = 'ckan.datastore.read_url' not in self.config

        # Check whether we are running one of the paster commands which means
        # that we should ignore the following tests.
        import sys
        if sys.argv[0].split('/')[-1] == 'paster' and 'datastore' in sys.argv[1:]:
            log.warn("Omitting permission checks because you are "
                     "running paster commands.")
            return

        self.ckan_url = self.config['sqlalchemy.url']
        self.write_url = self.config['ckan.datastore.write_url']
        if self.legacy_mode:
            self.read_url = self.write_url
        else:
            self.read_url = self.config['ckan.datastore.read_url']

        if model.engine_is_pg():
            if not self._is_read_only_database():
                # Make sure that the right permissions are set
                # so that no harmful queries can be made
                if not ('debug' in config and config['debug']):
                    if self._same_ckan_and_datastore_db():
                        raise Exception("The write and read-only database "
                                        "connection url are the same.")
                if self.legacy_mode:
                    log.warn("Legacy mode active. "
                             "The sql search will not be available.")
                elif not self._read_connection_has_correct_privileges():
                    if 'debug' in self.config and self.config['debug']:
                        log.critical("We have write permissions "
                                     "on the read-only database.")
                    else:
                        raise Exception("We have write permissions "
                                        "on the read-only database.")
                self._create_alias_table()
            else:
                log.warn("We detected that CKAN is running on a read "
                         "only database. Permission checks and the creation "
                         "of _table_metadata are skipped.")
        else:
            log.warn("We detected that you do not use a PostgreSQL "
                     "database. The DataStore will NOT work and datastore "
                     "tests will be skipped.")

        ## Do light wrapping around action function to add datastore_active
        ## to resource dict.  Not using IAction extension as this prevents
        ## other plugins from having a custom resource_read.

        # Make sure actions are cached
        resource_show = p.toolkit.get_action('resource_show')

        def new_resource_show(context, data_dict):
            engine = db._get_engine(
                context,
                {'connection_url': self.read_url}
            )
            new_data_dict = resource_show(context, data_dict)
            try:
                connection = engine.connect()
                result = connection.execute(
                    'SELECT 1 FROM "_table_metadata" WHERE name = %s AND alias_of IS NULL',
                    new_data_dict['id']
                ).fetchone()
                if result:
                    new_data_dict['datastore_active'] = True
                else:
                    new_data_dict['datastore_active'] = False
            finally:
                connection.close()
            return new_data_dict

        ## Make sure do not run many times if configure is called repeatedly
        ## as in tests.
        if not hasattr(resource_show, '_datastore_wrapped'):
            new_resource_show._datastore_wrapped = True
            logic._actions['resource_show'] = new_resource_show
        self._add_is_valid_type_function()

    def _is_read_only_database(self):
        '''
        Returns True if no connection has CREATE privileges on the public
        schema. This is the case if replication is enabled.
        '''
        for url in [self.ckan_url, self.write_url, self.read_url]:
            connection = db._get_engine(None,
                                        {'connection_url': url}).connect()
            sql = u"SELECT has_schema_privilege('public', 'CREATE')"
            is_writable = connection.execute(sql).first()[0]
            if is_writable:
                return False
        return True

    def _same_ckan_and_datastore_db(self):
        '''
        Make sure the datastore is on a separate db. Otherwise one could access
        all internal tables via the api.

        Returns True if the CKAN and DataStore db are the same
        '''

        if not self.legacy_mode:
            if self.write_url == self.read_url:
                return True

        if self._get_db_from_url(self.ckan_url) == self._get_db_from_url(self.read_url):
            return True
        return False

    def _get_db_from_url(self, url):
        return url[url.rindex("@"):]

    def _read_connection_has_correct_privileges(self):
        '''
        Returns True if the right permissions are set for the read only user.
        A table is created by the write user to test the read only user.
        '''
        write_connection = db._get_engine(None,
            {'connection_url': self.write_url}).connect()
        write_connection.execute(
            u"DROP TABLE IF EXISTS public._foo;",
            u"CREATE TABLE public._foo ()")

        read_connection = db._get_engine(None,
            {'connection_url': self.read_url}).connect()

        try:
            write_connection.execute(u"CREATE TABLE public._foo ()")
            for privilege in ['INSERT', 'UPDATE', 'DELETE']:
                sql = u"SELECT has_table_privilege('_foo', '{privilege}')".format(privilege=privilege)
                have_privilege = read_connection.execute(sql).first()[0]
                if have_privilege:
                    return False
        finally:
            write_connection.execute("DROP TABLE _foo")
        return True

    def _create_alias_table(self):
        mapping_sql = '''
            SELECT DISTINCT
                substr(md5(dependee.relname || COALESCE(dependent.relname, '')), 0, 17) AS "_id",
                dependee.relname AS name,
                dependee.oid AS oid,
                dependent.relname AS alias_of
                -- dependent.oid AS oid
            FROM
                pg_class AS dependee
                LEFT OUTER JOIN pg_rewrite AS r ON r.ev_class = dependee.oid
                LEFT OUTER JOIN pg_depend AS d ON d.objid = r.oid
                LEFT OUTER JOIN pg_class AS dependent ON d.refobjid = dependent.oid
            WHERE
                (dependee.oid != dependent.oid OR dependent.oid IS NULL) AND
                (dependee.relname IN (SELECT tablename FROM pg_catalog.pg_tables)
                    OR dependee.relname IN (SELECT viewname FROM pg_catalog.pg_views)) AND
                dependee.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname='public')
            ORDER BY dependee.oid DESC;
        '''
        create_alias_table_sql = u'CREATE OR REPLACE VIEW "_table_metadata" AS {0}'.format(mapping_sql)
        connection = db._get_engine(None,
            {'connection_url': pylons.config['ckan.datastore.write_url']}).connect()
        connection.execute(create_alias_table_sql)

    def _add_is_valid_type_function(self):
        # syntax_error - may occur if someone provides a keyword as a type
        # undefined_object - is raised if the type does not exist
        create_func_sql = '''
        CREATE OR REPLACE FUNCTION is_valid_type(v_type text)
        RETURNS boolean
        AS $$
        BEGIN
            PERFORM v_type::regtype;
            RETURN true;
        EXCEPTION WHEN undefined_object OR syntax_error THEN
            RETURN false;
        END;
        $$ LANGUAGE plpgsql stable;
        '''
        connection = db._get_engine(None,
            {'connection_url': pylons.config['ckan.datastore.write_url']}).connect()
        connection.execute(create_func_sql)

    def get_actions(self):
        actions = {'datastore_create': action.datastore_create,
                   'datastore_upsert': action.datastore_upsert,
                   'datastore_delete': action.datastore_delete,
                   'datastore_search': action.datastore_search}
        if not self.legacy_mode:
            actions['datastore_search_sql'] = action.datastore_search_sql
        return actions

    def get_auth_functions(self):
        return {'datastore_create': auth.datastore_create,
                'datastore_upsert': auth.datastore_upsert,
                'datastore_delete': auth.datastore_delete,
                'datastore_search': auth.datastore_search}
