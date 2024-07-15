import json
import urllib
from datetime import datetime

import cx_Oracle
from flask import g, current_app

from namex.models import Event, Request, State, User
from namex.services import EventRecorder
from namex.services.nro import NROServicesError

from .exceptions import NROServicesError
from .request_utils import (
    add_applicant,
    add_comments,
    add_names,
    add_nr_header,
    add_nwpta,
    get_exam_comments,
    get_names,
    get_nr_header,
    get_nr_requester,
    get_nr_submitter,
    get_nwpta,
)
from .utils import nro_examiner_name


class NROServices(object):
    """Provides services to change the legacy NRO Database
       For ease of use, following the style of a Flask Extension
    """

    def __init__(self, app=None):
        """initializer, supports setting the app context on instantiation"""
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        """setup for the extension
        :param app: Flask app
        :return: naked
        """
        self.app = app
        app.teardown_appcontext(self.teardown)

    def teardown(self, exception):
        # the oracle session pool will clean up after itself
        db_pool = g.pop('nro_oracle_pool', None)
        if db_pool is not None:
            db_pool.close()

    def _create_pool(self):
        """create the cx_oracle connection pool from the Flask Config Environment

        :return: an instance of the OCI Session Pool
        """
        # this uses the builtin session / connection pooling provided by
        # the Oracle OCI driver
        # setting threaded =True wraps the underlying calls in a Mutex
        # so we don't have to that here


        def InitSession(conn, requestedTag):
            cursor = conn.cursor()
            cursor.execute("alter session set TIME_ZONE = 'America/Vancouver'")

        user = current_app.config.get('NRO_USER')
        password = current_app.config.get('NRO_PASSWORD')
        host = current_app.config.get('NRO_HOST')
        port = current_app.config.get('NRO_PORT')
        db_name = current_app.config.get('NRO_DB_NAME')
        return cx_Oracle.SessionPool(user=user,
                                     password=password,
                                     dsn=f'{host}:{port}/{db_name}',
                                     min=1,
                                     max=10,
                                     increment=1,
                                     connectiontype=cx_Oracle.Connection,
                                     threaded=True,
                                     getmode=cx_Oracle.SPOOL_ATTRVAL_NOWAIT,
                                     waitTimeout=1500,
                                     timeout=3600,
                                     sessionCallback=InitSession)

    @property
    def connection(self):
        """connection property of the NROService
        If this is running in a Flask context,
        then either get the existing connection pool or create a new one
        and then return an acquired session
        :return: cx_Oracle.connection type
        """
        if 'nro_oracle_pool' not in g:
            g._nro_oracle_pool = self._create_pool()
        return g._nro_oracle_pool.acquire()


    def get_last_update_timestamp(self, nro_request_id):
        """Gets a datetime object that holds the last time and part of the NRO Request was modified

        :param nro_request_id: NRO request.request_id for the request we want to enquire about \
                               it DOES NOT use the nr_num, as that requires yet another %^&$# join and runs a \
                               couple of orders of magnitude slower. (really nice db design - NOT)
        :return: (datetime) the last time that any part of the request was altered
        :raise: (NROServicesError) with the error information set
        """

        try:
            cursor = self.connection.cursor()

            cursor.execute("""
                SELECT SYS_EXTRACT_UTC (cast(last_update as timestamp)) as last_update
                FROM req_instance_max_event
                WHERE request_id = :req_id"""
                ,req_id=nro_request_id)

            row = cursor.fetchone()

            if row:
                return row[0]

            return None

        except Exception as err:
            current_app.logger.error(err.with_traceback(None))
            raise NROServicesError({"code": "unable_to_get_timestamp",
                                    "description": "Unable to get the last timestamp for the NR in NRO"}, 500)

    def get_current_request_state(self, nro_nr_num):
        """Gets a datetime object that holds the last time and part of the NRO Request was modified

        :param nro_request_id: NRO request.request_id for the request we want to enquire about \
                               it DOES NOT use the nr_num, as that requires yet another %^&$# join and runs a \
                               couple of orders of magnitude slower. (really nice db design - NOT)
        :return: (datetime) the last time that any part of the request was altered
        :raise: (NROServicesError) with the error information set
        """

        try:
            cursor = self.connection.cursor()

            cursor.execute("""
                          select rs.STATE_TYPE_CD
                            from request_state rs
                            join request r on rs.request_id=r.request_id
                           where r.nr_num=:req_num
                             and rs.end_event_id is NULL"""
                    ,req_num=nro_nr_num)

            row = cursor.fetchone()

            if row:
                return row[0]

            return None

        except Exception as err:
            current_app.logger.error(err.with_traceback(None))
            raise NROServicesError({"code": "unable_to_get_request_state",
                                    "description": "Unable to get the current state of the NRO Request"}, 500)

    def set_request_status_to_h(self, nr_num, examiner_username ):
        """Sets the status of the Request in NRO to "H"

        :param nr_num: (str) the name request number, of the format "NR 9999999"
        :param examiner_username: (str) any valid string will work, but it should be the username from Keycloak
        :return: naked
        :raise: (NROServicesError) with the error information set
        """

        try:
            con = self.connection
            con.begin() # explicit transaction in case we need to do other things than just call the stored proc
            try:
                cursor = con.cursor()

                func_name = 'nro_datapump_pkg.name_examination_func'

                func_vars = [nr_num,           # p_nr_number
                            'H',               # p_status
                            '',               # p_expiry_date - mandatory, but ignored by the proc
                            '',               # p_consent_flag- mandatory, but ignored by the proc
                            nro_examiner_name(examiner_username), # p_examiner_id
                            ]

                # Call the name_examination function to save complete decision data for a single NR
                # and get a return if all data was saved
                ret = cursor.callfunc(func_name, str, func_vars)
                if ret is not None:
                    current_app.logger.error('name_examination_func failed, return message: {}'.format(ret))
                    raise NROServicesError({"code": "unable_to_set_state", "description": ret}, 500)

                con.commit()

            except cx_Oracle.DatabaseError as exc:
                error, = exc.args
                current_app.logger.error("NR#: %s Oracle-Error-Code: %s Oracle-Error-Message: %s", nr_num, error.code, error.message)
                if con:
                    con.rollback()
                raise NROServicesError({"code": "unable_to_set_state",
                        "description": "Unable to set the state of the NR in NRO"}, 500)
            except Exception as err:
                current_app.logger.error("NR#:", nr_num, err.with_traceback(None))
                if con:
                    con.rollback()
                raise NROServicesError({"code": "unable_to_set_state",
                                        "description": "Unable to set the state of the NR in NRO"}, 500)

        except Exception as err:
            # something went wrong, roll it all back
            current_app.logger.error("NR#:", nr_num, err.with_traceback(None))
            raise NROServicesError({"code": "unable_to_set_state",
                                    "description": "Unable to set the state of the NR in NRO"}, 500)

        return None


    def change_nr(self, nr, change_flags):

        warnings = []

        # save the current state, as we'll need to set it back to this before returning
        nr_saved_state = nr.stateCd

        try:

            con = self.connection
            con.begin()  # explicit transaction in case we need to do other things than just call the stored proc

            cursor = con.cursor()
            update_nr(nr, cursor, change_flags,con)

            con.commit()

            return None

        except Exception as err:
            warnings.append({'type': 'warn',
                             'code': 'unable_to_update_request_changes_in_NRO',
                             'message': 'Unable to update the Request details in NRO,'
                                        ' please manually verify record is up to date in NRO before'
                                        ' continuing.'
                             })
            current_app.logger.error(err.with_traceback(None))

        finally:
            # set the NR back to its initial state
            # nr.stateCd = State.INPROGRESS
            nr.stateCd = nr_saved_state
            nr.save_to_db()

        return warnings if len(warnings)>0 else None


    def checkin_checkout_nr(self, nr, action):
        warnings = []
        try:

            con = self.connection
            con.begin()  # explicit transaction in case we need to do other things than just call the stored proc

            cursor = con.cursor()
            manage_nr_locks(nr, cursor, action, con)

            con.commit()

            return None

        except Exception as err:
            warnings.append({'type': 'warn',
                             'code': 'unable_to_update_request_changes_in_NRO',
                             'message': 'Unable to update the Request details in NRO,'
                                        ' please manually verify record is up to date in NRO before'
                                        ' continuing.'
                             })
            current_app.logger.error(err.with_traceback(None))

        return warnings if len(warnings) > 0 else None



    def cancel_nr(self, nr, examiner_username):
        """Sets the status of the Request in NRO to "C" (Cancelled)

        :param nr: (obj) NR Object
        :param examiner_username: (str) any valid string will work, but it should be the username from Keycloak
        :return: naked
        :raise: (NROServicesError) with the error information set
        """

        try:
            con = self.connection
            con.begin() # explicit transaction in case we need to do other things than just call the stored proc
            try:
                cursor = con.cursor()

                event_id = _get_event_id(cursor)
                current_app.logger.debug('got to cancel_nr() for NR:{}'.format(nr.nrNum))
                current_app.logger.debug('event ID for NR:{}'.format(event_id))
                _create_nro_transaction(cursor, nr, event_id, 'CANCL')

                # get request_state record, with all fields
                cursor.execute("""
                SELECT *
                FROM request_state
                WHERE request_id = :request_id
                AND end_event_id IS NULL
                FOR UPDATE
                """,
                                      request_id=nr.requestId)
                row = cursor.fetchone()
                req_state_id = int(row[0])

                # set the end event for the existing record
                cursor.execute("""
                UPDATE request_state
                SET end_event_id = :event_id
                WHERE request_state_id = :req_state_id
                """,
                                      event_id=event_id,
                                      req_state_id=req_state_id)

                # create new request_state record
                cursor.execute("""
                INSERT INTO request_state (request_state_id, request_id, state_type_cd,
                    start_event_id, end_event_id, examiner_idir, examiner_comment, state_comment,
                    batch_id)
                VALUES (request_state_seq.nextval, :request_id, :state, :event_id, NULL,
                          :examiner_id, NULL, NULL, NULL)
                """,
                                      request_id=nr.requestId,
                                      state='C',
                                      event_id=event_id,
                                      examiner_id=nro_examiner_name(examiner_username)
                                      )

                con.commit()

            except cx_Oracle.DatabaseError as exc:
                err, = exc.args
                current_app.logger.error(err)
                if con:
                    con.rollback()
                raise NROServicesError({"code": "unable_to_set_state",
                        "description": "Unable to set the state of the NR in NRO"}, 500)
            except Exception as err:
                current_app.logger.error(err.with_traceback(None))
                if con:
                    con.rollback()
                raise NROServicesError({"code": "unable_to_set_state",
                        "description": "Unable to set the state of the NR in NRO"}, 500)

        except Exception as err:
            # something went wrong, roll it all back
            current_app.logger.error(err.with_traceback(None))
            if con:
                con.rollback()
            raise NROServicesError({"code": "unable_to_set_state",
                        "description": "Unable to set the state of the NR in NRO"}, 500)

        return None

    def fetch_nro_request_and_copy_to_namex_request(self, user: User, nr_number: str, name_request: Request = None) \
            -> Request:
        """Utility function to gather up and copy a Request from NRO to a NameX Request Object
           The request is NOT persisted in this helper method
        """
        try:
            cursor = self.connection.cursor()

            if name_request:
                nr = name_request
                nr_num = nr.nrNum
            else:
                nr_num = nr_number
                nr = Request.find_by_nr(nr_num)
                if not nr:
                    nr = Request()

            nr_header = get_nr_header(cursor, nr_num)

            if not nr_header:
                current_app.logger.info('Attempting to fetch Request:{} from NRO, but does not exist'.format(nr_num))
                return None
            current_app.logger.debug('fetched nr_header: {}'.format(nr_header))

            # get all the request segments from NRO
            nr_submitter = get_nr_submitter(cursor, nr_header['request_id'])
            nr_applicant = get_nr_requester(cursor, nr_header['request_id'])
            nr_ex_comments = get_exam_comments(cursor, nr_header['request_id'])
            nr_nwpta = get_nwpta(cursor, nr_header['request_id'])
            nr_names = get_names(cursor, nr_header['request_id'])

            current_app.logger.debug('completed all gets')

        except Exception as err:
            current_app.logger.debug('unable to load nr_header: {}'.format(nr_num), err.with_traceback(None))
            return None

        add_nr_header(nr, nr_header, nr_submitter, user)
        current_app.logger.debug('completed header for {}'.format(nr.nrNum))
        nr.add_to_db()
        if nr_applicant:
            add_applicant(nr, nr_applicant)
            current_app.logger.debug('completed applicants for {}'.format(nr.nrNum))
        if nr_ex_comments:
            add_comments(nr, nr_ex_comments)
            current_app.logger.debug('completed comments for {}'.format(nr.nrNum))
        if nr_nwpta:
            add_nwpta(nr, nr_nwpta)
            current_app.logger.debug('completed nwpta for {}'.format(nr.nrNum))
        if nr_names:
            current_app.logger.debug('nr_names data into add_names():')
            current_app.logger.debug(nr_names)
            add_names(nr, nr_names)
            current_app.logger.debug('completed names for {}'.format(nr.nrNum))

        return nr
