#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""This library is an attempt to implement the LSV2 communication protocol used by certain
   CNC controls.
   Please consider the dangers of using this library on a production machine! This library is
   by no means complete and could damage the control or cause injuries! Everything beyond simple
   file manipulation is blocked by a lockout parameter. Use at your own risk!
"""
import logging
import re
import os
import struct
from datetime import datetime
from pathlib import Path

from .const import (
    ControlType,
    DriveName,
    Login,
    MemoryType,
    LSV2Err,
    CMD,
    RSP,
    ParCCC,
    ParRVR,
    ParRRI,
    ParRDR,
    BIN_FILES,
    PATH_SEP,
    MODE_BINARY,
)

from .low_level_com import LLLSV2Com
from .misc import (
    decode_directory_info,
    decode_error_message,
    decode_file_system_info,
    decode_override_information,
    decode_system_parameters,
    decode_tool_information,
    is_file_binary,
)
from .translate_messages import (
    get_error_text,
    get_execution_status_text,
    get_program_status_text,
)


class LSV2:
    """Implementation of the LSV2 protocol used to communicate with certain CNC controls"""

    def __init__(
        self, hostname, port=0, timeout=15.0, safe_mode=True, locale_path=None, enc_errors='ignore', enc_encoding='utf-8'
    ):
        """init object variables and create socket"""
        logging.getLogger(__name__).addHandler(logging.NullHandler())

        self._llcom = LLLSV2Com(hostname, port, timeout)

        self._buffer_size = LLLSV2Com.DEFAULT_BUFFER_SIZE
        self._active_logins = list()

        if safe_mode:
            logging.info(
                "safe mode is active, login and system commands are restricted"
            )
            self._known_logins = (Login.INSPECT, Login.FILETRANSFER, Login.MONITOR)
            self._known_sys_cmd = (
                ParCCC.SET_BUF1024,
                ParCCC.SET_BUF512,
                ParCCC.SET_BUF2048,
                ParCCC.SET_BUF3072,
                ParCCC.SET_BUF4096,
                ParCCC.SECURE_FILE_SEND,
                ParCCC.SCREENDUMP,
            )
        else:
            logging.info(
                "safe mode is off, login and system commands are not restricted. Use with caution!"
            )
            self._known_logins = [e.value for e in Login]
            self._known_sys_cmd = [e.value for e in ParCCC]

        self._versions = None
        self._sys_par = None
        self._secure_file_send = False
        self._control_type = ControlType.UNKNOWN
        self._last_error_code = None

        self._enc_errors = enc_errors
        self._enc_encoding = enc_encoding

        if locale_path is None:
            self._locale_path = os.path.join(os.path.dirname(__file__), "locales")
        else:
            self._locale_path = locale_path

    def connect(self):
        """connect to control"""
        self._llcom.connect()
        self._configure_connection()

    def disconnect(self):
        """logout of all open logins and close connection"""
        self.logout(login=None)
        self._llcom.disconnect()
        logging.debug("Connection to host closed")

    def is_itnc(self):
        """return true if control is a iTNC"""
        return self._control_type == ControlType.MILL_OLD

    def is_tnc(self):
        """return true if control is a TNC"""
        return self._control_type == ControlType.MILL_NEW

    def is_pilot(self):
        """return true if control is a CNCPILOT640"""
        return self._control_type == ControlType.LATHE_NEW

    @staticmethod
    def _decode_error(content, locale_path=None):
        """decode error codes to text"""
        if locale_path is None:
            locale_path = os.path.join(os.path.dirname(__file__), "locales")
        (
            byte_1,
            byte_2,
        ) = struct.unpack("!BB", content)
        error_text = get_error_text(byte_1, byte_2, locale_path=locale_path)
        logging.warning(
            "T_ER or T_BD received, an error occurred during the execution of the last command: %s",
            error_text,
        )
        return error_text

    def _send_recive(self, command, expected_response, payload=None):
        """takes a command and payload, sends it to the control and checks
        if the response is as expected. Returns content if not an error"""
        if expected_response is None:
            self._llcom.telegram(
                command, payload, buffer_size=self._buffer_size, wait_for_response=False
            )
            logging.info(
                "command %s sent successfully, did not check for response", command
            )
            return True
        else:
            response, content = self._llcom.telegram(
                command, payload, buffer_size=self._buffer_size, wait_for_response=True
            )

            if response in expected_response:
                if content is not None and len(content) > 0:
                    logging.debug(
                        "command %s executed successfully, received %s with %d bytes payload",
                        command,
                        response,
                        len(content),
                    )
                    return content
                logging.debug(
                    "command %s executed successfully, received %s without any payload",
                    command,
                    response,
                )
                return True

            if response in RSP.T_ER:
                self._decode_error(content)
                self._last_error_code = struct.unpack("!BB", content)
            else:
                logging.error(
                    "received unexpected response %s to command %s. response code %s",
                    response,
                    command,
                    content,
                )
                self._last_error_code = None

        return False

    def _send_recive_block(self, command, expected_response, payload=None):
        """takes a command and payload, sends it to the control and continues reading
        until the expected response is received."""
        response_buffer = list()
        response, content = self._llcom.telegram(
            command, payload, buffer_size=self._buffer_size
        )

        if response in RSP.T_ER:
            self._decode_error(content)
        elif response in RSP.T_FD:
            logging.debug("Transfer is finished with no content")
        elif response not in expected_response:
            logging.error(
                "received unexpected response %s block read for command %s. response code %s",
                response,
                command,
                content,
            )
            raise Exception("received unexpected response {}".format(response))
        else:
            while response in expected_response:
                response_buffer.append(content)
                response, content = self._llcom.telegram(
                    RSP.T_OK, buffer_size=self._buffer_size
                )
        return response_buffer

    def _send_recive_ack(self, command, payload=None):
        """sends command and pyload to control, returns True on T_OK"""
        response, content = self._llcom.telegram(
            command, payload, buffer_size=self._buffer_size
        )
        if response in RSP.T_OK:
            return True

        if response in RSP.T_ER:
            self._decode_error(content)
        else:
            logging.error(
                "received unexpected response %s to command %s. response code %s",
                response,
                command,
                content,
            )
        return False

    def _configure_connection(self):
        """Set up the communication parameters for file transfer. Buffer size and secure file
        transfere are enabled based on the capabilitys of the control.

        :rtype: None
        """
        self.login(login=Login.INSPECT)
        control_type = self.get_versions()["Control"]
        max_block_length = self.get_system_parameter()["Max_Block_Length"]
        logging.info(
            "setting connection settings for %s and block length %s",
            control_type,
            max_block_length,
        )

        if control_type in ("TNC640", "TNC620", "TNC320", "TNC128"):
            self._control_type = ControlType.MILL_NEW
        elif control_type in ("iTNC530", "iTNC530 Programm"):
            self._control_type = ControlType.MILL_OLD
        elif control_type in ("CNCPILOT640",):
            self._control_type = ControlType.LATHE_NEW
        else:
            logging.warning("Unknown control type, treat machine as new style mill")
            self._control_type = ControlType.MILL_NEW

        selected_size = -1
        selected_command = None
        if max_block_length >= 4096:
            selected_size = 4096
            selected_command = ParCCC.SET_BUF4096
        elif 3072 <= max_block_length < 4096:
            selected_size = 3072
            selected_command = ParCCC.SET_BUF3072
        elif 2048 <= max_block_length < 3072:
            selected_size = 2048
            selected_command = ParCCC.SET_BUF2048
        elif 1024 <= max_block_length < 2048:
            selected_size = 1024
            selected_command = ParCCC.SET_BUF1024
        elif 512 <= max_block_length < 1024:
            selected_size = 512
            selected_command = ParCCC.SET_BUF512
        elif 256 <= max_block_length < 512:
            selected_size = 256
        else:
            logging.error(
                "could not decide on a buffer size for maximum message length of %d",
                max_block_length,
            )
            raise Exception("unknown buffer size")

        if selected_command is None:
            logging.debug("use smallest buffer size of 256")
            self._buffer_size = selected_size
        else:
            logging.debug("use buffer size of %d", selected_size)
            if self.set_system_command(selected_command):
                self._buffer_size = selected_size
            else:
                raise Exception(
                    "error in communication while setting buffer size to %d"
                    % selected_size
                )

        if not self.set_system_command(ParCCC.SECURE_FILE_SEND):
            logging.warning("secure file transfer not supported? use fallback")
            self._secure_file_send = False
        else:
            self._secure_file_send = True

        self.login(login=Login.FILETRANSFER)
        logging.info(
            "successfully configured connection parameters and basic logins. selected buffer size is %d, use secure file send: %s",
            self._buffer_size,
            self._secure_file_send,
        )

    def login(self, login, password=None):
        """Request additional access rights. To elevate this level a logon has to be performed. Some levels require a password.

        :param str login: One of the known login strings
        :param str password: optional. Password for login
        :returns: True if execution was successful
        :rtype: bool
        """
        if login in self._active_logins:
            logging.debug("login already active")
            return True

        if login not in self._known_logins:
            logging.error("unknown or unsupported login")
            return False

        payload = bytearray()
        payload.extend(map(ord, login))
        payload.append(0x00)
        if password is not None:
            payload.extend(map(ord, password))
            payload.append(0x00)

        if not self._send_recive_ack(CMD.A_LG, payload):
            logging.error("an error occurred during login for login %s", login)
            return False

        self._active_logins.append(login)

        logging.info("login executed successfully for login %s", login)
        return True

    def logout(self, login=None):
        """Drop one or all access right. If no login is supplied all active access rights are dropped.

        :param str login: optional. One of the known login strings
        :returns: True if execution was successful
        :rtype: bool
        """
        if login in self._known_logins or login is None:
            logging.debug("logout for login %s", login)
            if login in self._active_logins or login is None:
                payload = bytearray()
                if login is not None:
                    payload.extend(map(ord, login))
                    payload.append(0x00)

                if self._send_recive_ack(CMD.A_LO, payload):
                    logging.info("logout executed successfully for login %s", login)
                    if login is not None:
                        self._active_logins.remove(login)
                    else:
                        self._active_logins = list()
                    return True
            else:
                logging.info("login %s was not active, logout not necessary", login)
                return True
        else:
            logging.warning("unknown or unsupported user")
        return False

    def set_system_command(self, command, parameter=None):
        """Execute a system command on the control if command is one a known value. If safe mode is active, some of the
        commands are disabled. If necessary additinal parameters can be supplied.

        :param int command: system command
        :param str parameter: optional. parameter payload for system command
        :returns: True if execution was successful
        :rtype: bool
        """
        if command in self._known_sys_cmd:
            payload = bytearray()
            payload.extend(struct.pack("!H", command))
            if parameter is not None:
                payload.extend(map(ord, parameter))
                payload.append(0x00)
            if self._send_recive_ack(CMD.C_CC, payload):
                return True
        logging.debug("unknown or unsupported system command")
        return False

    def get_system_parameter(self, force=False):
        """Get all version information, result is bufferd since it is also used internally. With parameter force it is
        possible to manually re-read the information form the control

        :param bool force: if True the information is read even if it is already buffered
        :returns: dictionary with system parameters like number of plc variables, supported lsv2 version etc.
        :rtype: dict
        """
        if self._sys_par is not None and force is False:
            logging.debug("version info already in memory, return previous values")
            return self._sys_par

        result = self._send_recive(command=CMD.R_PR, expected_response=RSP.S_PR)
        if result:
            sys_par = decode_system_parameters(result)
            logging.debug("got system parameters: %s", sys_par)
            self._sys_par = sys_par
            return self._sys_par
        logging.error("an error occurred while querying system parameters")
        return False

    def get_versions(self, force=False):
        """Get all version information, result is bufferd since it is also used internally. With parameter force it is
        possible to manually re-read the information form the control

        :param bool force: if True the information is read even if it is already buffered
        :returns: dictionary with version text for control type, nc software, plc software, software options etc.
        :rtype: dict
        """
        if self._versions is not None and force is False:
            logging.debug("version info already in memory, return previous values")
        else:
            info_data = dict()

            result = self._send_recive(
                CMD.R_VR, RSP.S_VR, payload=struct.pack("!B", ParRVR.CONTROL)
            )
            if result:
                info_data["Control"] = result.strip(b"\x00").decode("utf-8")
            else:
                raise Exception("Could not read version information from control")

            result = self._send_recive(
                CMD.R_VR, RSP.S_VR, payload=struct.pack("!B", ParRVR.NC_VERSION)
            )
            if result:
                info_data["NC_Version"] = result.strip(b"\x00").decode("utf-8")

            result = self._send_recive(
                CMD.R_VR, RSP.S_VR, payload=struct.pack("!B", ParRVR.PLC_VERSION)
            )
            if result:
                info_data["PLC_Version"] = result.strip(b"\x00").decode("utf-8")

            result = self._send_recive(
                CMD.R_VR, RSP.S_VR, payload=struct.pack("!B", ParRVR.OPTIONS)
            )
            if result:
                info_data["Options"] = result.strip(b"\x00").decode("utf-8")

            result = self._send_recive(
                CMD.R_VR, RSP.S_VR, payload=struct.pack("!B", ParRVR.ID)
            )
            if result:
                info_data["ID"] = result.strip(b"\x00").decode("utf-8")

            if self.is_itnc():
                info_data["Release_Type"] = "not supported"
            else:
                result = self._send_recive(
                    CMD.R_VR, RSP.S_VR, payload=struct.pack("!B", ParRVR.RELEASE_TYPE)
                )
                if result:
                    info_data["Release_Type"] = result.strip(b"\x00").decode("utf-8")

            result = self._send_recive(
                CMD.R_VR, RSP.S_VR, payload=struct.pack("!B", ParRVR.SPLC_VERSION)
            )
            if result:
                info_data["SPLC_Version"] = result.strip(b"\x00").decode("utf-8")
            else:
                info_data["SPLC_Version"] = "not supported"

            logging.debug("got version info: %s", info_data)
            self._versions = info_data

        return self._versions

    def get_program_status(self):
        """Get status code of currently active program
        See https://github.com/tfischer73/Eclipse-Plugin-Heidenhain/issues/1

        :returns: status code or False if something went wrong
        :rtype: int
        """
        self.login(login=Login.DNC)

        payload = bytearray()
        payload.extend(struct.pack("!H", ParRRI.PGM_STATE))

        result = self._send_recive(CMD.R_RI, RSP.S_RI, payload)
        if result:
            pgm_state = struct.unpack("!H", result)[0]
            logging.debug(
                "successfully read state of active program: %s",
                get_program_status_text(pgm_state, locale_path=self._locale_path),
            )
            return pgm_state

        logging.error("an error occurred while querying program state")
        return False

    def get_program_stack(self):
        """Get path of currently active nc program(s) and current line number
        See https://github.com/tfischer73/Eclipse-Plugin-Heidenhain/issues/1

        :returns: dictionary with line number, main program and current program or False if something went wrong
        :rtype: dict
        """
        self.login(login=Login.DNC)

        payload = bytearray()
        payload.extend(struct.pack("!H", ParRRI.SELECTED_PGM))

        result = self._send_recive(CMD.R_RI, RSP.S_RI, payload=payload)
        if result:
            stack_info = dict()
            stack_info["Line"] = struct.unpack("!L", result[:4])[0]
            stack_info["Main_PGM"] = (
                result[4:]
                .split(b"\x00")[0]
                .decode(encoding = self._enc_encoding, errors = self._enc_errors)
                .strip("\x00")  # .replace("\\", "/")
            )
            stack_info["Current_PGM"] = (
                result[4:]
                .split(b"\x00")[1]
                .decode(encoding = self._enc_encoding, errors = self._enc_errors)
                .strip("\x00")  # .replace("\\", "/")
            )
            logging.debug(
                "successfully read active program stack and line number: %s", stack_info
            )
            return stack_info

        logging.error("an error occurred while querying active program state")
        return False

    def get_execution_status(self):
        """Get status code of program state to text
        See https://github.com/drunsinn/pyLSV2/issues/1

        :returns: status code or False if something went wrong
        :rtype: int
        """
        self.login(login=Login.DNC)

        payload = bytearray()
        payload.extend(struct.pack("!H", ParRRI.EXEC_STATE))

        result = self._send_recive(CMD.R_RI, RSP.S_RI, payload)
        if result:
            exec_state = struct.unpack("!H", result)[0]
            logging.debug(
                "read execution state %d : %s",
                exec_state,
                get_execution_status_text(exec_state, locale_path=self._locale_path),
            )
            return exec_state

        logging.error("an error occurred while querying execution state")
        return False

    def get_directory_info(self, remote_directory=None):
        """Query information a the current working directory on the control

        :param str remote_directory: optional. If set, working directory will be changed
        :returns: dictionary with info about the directory or False if an error occurred
        :rtype: dict
        """
        if remote_directory is not None and not self.change_directory(remote_directory):
            logging.error(
                "could not change current directory to read directory info for %s",
                remote_directory,
            )

        result = self._send_recive(CMD.R_DI, RSP.S_DI)
        if result:
            dir_info = decode_directory_info(result)
            logging.debug("successfully received directory information %s", dir_info)

            return dir_info

        logging.error("an error occurred while querying directory info")
        return False

    def change_directory(self, remote_directory):
        """Change the current working directoyon the control

        :param str remote_directory: path of directory on the control
        :returns: True if changing of directory succeeded
        :rtype: bool
        """
        dir_path = remote_directory.replace("/", PATH_SEP)
        payload = bytearray()
        payload.extend(map(ord, dir_path))
        payload.append(0x00)
        if self._send_recive_ack(CMD.C_DC, payload=payload):
            logging.debug("changed working directory to %s", dir_path)
            return True

        logging.error("an error occurred while changing directory")
        return False

    def get_file_info(self, remote_file_path):
        """Query information about a file

        :param str remote_file_path: path of file on the control
        :returns: dictionary with info about file of False if remote path does not exist
        :rtype: dict
        """
        file_path = remote_file_path.replace("/", PATH_SEP)
        payload = bytearray()
        payload.extend(map(ord, file_path))
        payload.append(0x00)

        result = self._send_recive(CMD.R_FI, RSP.S_FI, payload=payload)
        if result:
            file_info = decode_file_system_info(result, self._control_type)
            logging.debug("successfully received file information %s", file_info)
            return file_info

        logging.warning(
            "an error occurred while querying file info this might also indicate that it does not exist %s",
            remote_file_path,
        )
        return False

    def get_directory_content(self):
        """Query content of current working directory from the control.
        In some situations it is necessary to fist call get_directory_info() or else
        the attributes won't be correct.

        :returns: list of dict with info about directory entries
        :rtype: list
        """
        dir_content = list()
        payload = bytearray()
        payload.append(ParRDR.SINGLE)

        result = self._send_recive_block(CMD.R_DR, RSP.S_DR, payload)
        logging.debug(
            "received %d entries for directory content information", len(result)
        )
        for entry in result:
            dir_content.append(decode_file_system_info(entry, self._control_type))

        logging.debug("successfully received directory information %s", dir_content)
        return dir_content

    def get_drive_info(self):
        """Query info all drives and partitions from the control

        :returns: list of dict with with info about drive entries
        :rtype: list
        """
        drives_list = list()
        payload = bytearray()
        payload.append(ParRDR.DRIVES)

        result = self._send_recive_block(CMD.R_DR, RSP.S_DR, payload)
        logging.debug("received %d packet of for drive information", len(result))
        for entry in result:
            drives_list.append(entry)

        logging.debug("successfully received drive information %s", drives_list)
        return drives_list

    def make_directory(self, dir_path):
        """Create a directory on control. If necessary also creates parent directories

        :param str dir_path: path of directory on the control
        :returns: True if creating of directory completed successfully
        :rtype: bool
        """
        path_parts = dir_path.replace("/", PATH_SEP).split(PATH_SEP)  # convert path
        path_to_check = ""
        for part in path_parts:
            path_to_check += part + PATH_SEP
            # no file info -> does not exist and has to be created
            if self.get_file_info(path_to_check) is False:
                payload = bytearray()
                payload.extend(map(ord, path_to_check))
                payload.append(0x00)  # terminate string
                if self._send_recive_ack(command=CMD.C_DM, payload=payload):
                    logging.debug("Directory created successfully")
                else:
                    raise Exception(
                        "an error occurred while creating directory {}".format(dir_path)
                    )
            else:
                logging.debug("nothing to do as this segment already exists")
        return True

    def delete_empty_directory(self, dir_path):
        """Delete empty directory on control

        :param str file_path: path of directory on the control
        :returns: True if deleting of directory completed successfully
        :rtype: bool
        """
        dir_path = dir_path.replace("/", PATH_SEP)
        payload = bytearray()
        payload.extend(map(ord, dir_path))
        payload.append(0x00)
        if not self._send_recive_ack(command=CMD.C_DD, payload=payload):
            logging.warning(
                "an error occurred while deleting directory %s, this might also indicate that it it does not exist",
                dir_path,
            )
            return False
        logging.debug("successfully deleted directory %s", dir_path)
        return True

    def delete_file(self, file_path):
        """Delete file on control

        :param str file_path: path of file on the control
        :returns: True if deleting of file completed successfully
        :rtype: bool
        """
        file_path = file_path.replace("/", PATH_SEP)
        payload = bytearray()
        payload.extend(map(ord, file_path))
        payload.append(0x00)
        if not self._send_recive_ack(command=CMD.C_FD, payload=payload):
            logging.warning(
                "an error occurred while deleting file %s, this might also indicate that it it does not exist",
                file_path,
            )
            return False
        logging.debug("successfully deleted file %s", file_path)
        return True

    def copy_local_file(self, source_path, target_path):
        """Copy file on control from one place to another

        :param str source_path: path of file on the control
        :param str target_path: path of target location
        :returns: True if copying of file completed successfully
        :rtype: bool
        """
        source_path = source_path.replace("/", PATH_SEP)
        target_path = target_path.replace("/", PATH_SEP)

        if PATH_SEP in source_path:
            # change directory
            source_file_name = source_path.split(PATH_SEP)[-1]
            source_directory = source_path.rstrip(source_file_name)
            if not self.change_directory(remote_directory=source_directory):
                raise Exception("could not open the source directory")
        else:
            source_file_name = source_path
            source_directory = "."

        if target_path.endswith(PATH_SEP):
            target_path += source_file_name

        payload = bytearray()
        payload.extend(map(ord, source_file_name))
        payload.append(0x00)
        payload.extend(map(ord, target_path))
        payload.append(0x00)
        logging.debug(
            "prepare to copy file %s from %s to %s",
            source_file_name,
            source_directory,
            target_path,
        )
        if not self._send_recive_ack(command=CMD.C_FC, payload=payload):
            logging.warning(
                "an error occurred copying file %s to %s", source_path, target_path
            )
            return False
        logging.debug("successfully copied file %s", source_path)
        return True

    def move_local_file(self, source_path, target_path):
        """Move file on control from one place to another

        :param str source_path: path of file on the control
        :param str target_path: path of target location
        :returns: True if moving of file completed successfully
        :rtype: bool
        """
        source_path = source_path.replace("/", PATH_SEP)
        target_path = target_path.replace("/", PATH_SEP)

        if PATH_SEP in source_path:
            source_file_name = source_path.split(PATH_SEP)[-1]
            source_directory = source_path.rstrip(source_file_name)
            if not self.change_directory(remote_directory=source_directory):
                raise Exception("could not open the source directory")
        else:
            source_file_name = source_path
            source_directory = "."

        if target_path.endswith(PATH_SEP):
            target_path += source_file_name

        payload = bytearray()
        payload.extend(map(ord, source_file_name))
        payload.append(0x00)
        payload.extend(map(ord, target_path))
        payload.append(0x00)
        logging.debug(
            "prepare to move file %s from %s to %s",
            source_file_name,
            source_directory,
            target_path,
        )
        if not self._send_recive_ack(command=CMD.C_FR, payload=payload):
            logging.warning(
                "an error occurred moving file %s to %s", source_path, target_path
            )
            return False
        logging.debug("successfully moved file %s", source_path)
        return True

    def send_file(
        self, local_path, remote_path, override_file=False, binary_mode=False
    ):
        """Upload a file to control

        :param str remote_path: path of file on the control
        :param str local_path: local path of destination with or without file name
        :param bool override_file: flag if file should be replaced if it already exists
        :param bool binary_mode: flag if binary transfer mode should be used, if not set the
                                 file name is checked for known binary file type
        :returns: True if transfer completed successfully
        :rtype: bool
        """
        local_file = Path(local_path)

        if not local_file.is_file():
            logging.error("the supplied path %s did not resolve to a file", local_file)
            raise Exception("local file does not exist! {}".format(local_file))

        remote_path = remote_path.replace("/", PATH_SEP)

        if PATH_SEP in remote_path:
            if remote_path.endswith(PATH_SEP):  # no filename given
                remote_file_name = local_file.name
                remote_directory = remote_path
            else:
                remote_file_name = remote_path.split(PATH_SEP)[-1]
                remote_directory = remote_path.rstrip(remote_file_name)
                if not self.change_directory(remote_directory=remote_directory):
                    raise Exception(
                        "could not open the source directory {}".format(
                            remote_directory
                        )
                    )
        else:
            remote_file_name = remote_path
            remote_directory = self.get_directory_info()["Path"]  # get pwd
        remote_directory = remote_directory.rstrip(PATH_SEP)

        if not self.get_directory_info(remote_directory):
            logging.debug("remote path does not exist, create directory(s)")
            self.make_directory(remote_directory)

        remote_info = self.get_file_info(remote_directory + PATH_SEP + remote_file_name)

        if remote_info:
            logging.debug("remote path exists and points to file's")
            if override_file:
                if not self.delete_file(remote_directory + PATH_SEP + remote_file_name):
                    raise Exception(
                        "something went wrong while deleting file {}".format(
                            remote_directory + PATH_SEP + remote_file_name
                        )
                    )
            else:
                logging.warning("remote file already exists, override was not set")
                return False

        logging.debug(
            "ready to send file from %s to %s",
            local_file,
            remote_directory + PATH_SEP + remote_file_name,
        )

        payload = bytearray()
        payload.extend(map(ord, remote_directory + PATH_SEP + remote_file_name))
        payload.append(0x00)
        if binary_mode or is_file_binary(local_path):
            payload.append(MODE_BINARY)
            logging.info("selecting binary transfer mode for this file type")
        else:
            payload.append(0x00)
            logging.info("selecting non binary transfer mode")

        response, content = self._llcom.telegram(
            CMD.C_FL, payload, buffer_size=self._buffer_size
        )

        if response in RSP.T_OK:
            with local_file.open("rb") as input_buffer:
                while True:
                    # use current buffer size but reduce by 10 to make sure it fits together with command and size
                    buffer = input_buffer.read(self._buffer_size - 10)
                    if not buffer:
                        # finished reading file
                        break

                    response, content = self._llcom.telegram(
                        RSP.S_FL, buffer, buffer_size=self._buffer_size
                    )
                    if response in RSP.T_OK:
                        pass
                    else:
                        if response in RSP.T_ER:
                            self._decode_error(content)
                        else:
                            logging.error("could not send data with error %s", response)
                        return False

            # signal that no more data is being sent
            if self._secure_file_send:
                if not self._send_recive(
                    command=RSP.T_FD, expected_response=RSP.T_OK, payload=None
                ):
                    logging.error("could not send end of file with error")
                    return False
            else:
                if not self._send_recive(
                    command=RSP.T_FD, expected_response=None, payload=None
                ):
                    logging.error("could not send end of file with error")
                    return False

        else:
            if response in RSP.T_ER:
                self._decode_error(content)
            else:
                logging.error("could not send file with error %s", response)
            return False

        return True

    def recive_file(
        self, remote_path, local_path, override_file=False, binary_mode=False
    ):
        """Download a file from control

        :param str remote_path: path of file on the control
        :param str local_path: local path of destination with or without file name
        :param bool override_file: flag if file should be replaced if it already exists
        :param bool binary_mode: flag if binary transfer mode should be used, if not set the file name is
                                 checked for known binary file type
        :returns: True if transfer completed successfully
        :rtype: bool
        """

        remote_path = remote_path.replace("/", PATH_SEP)
        remote_file_info = self.get_file_info(remote_path)
        if not remote_file_info:
            logging.error("remote file does not exist: %s", remote_path)
            return False

        local_file = Path(local_path)
        if local_file.is_dir():
            local_file.joinpath(remote_path.split("/")[-1])

        if local_file.is_file():
            logging.debug("local path exists and points to file")
            if override_file:
                local_file.unlink()
            else:
                logging.warning(
                    "local file already exists and override was not set. doing nothing"
                )
                return False

        logging.debug("loading file from %s to %s", remote_path, local_file)

        payload = bytearray()
        payload.extend(map(ord, remote_path))
        payload.append(0x00)
        if binary_mode or is_file_binary(remote_path):
            payload.append(MODE_BINARY)  # force binary transfer
            logging.info("using binary transfer mode")
        else:
            payload.append(0x00)
            logging.info("using non binary transfer mode")

        response, content = self._llcom.telegram(
            CMD.R_FL, payload, buffer_size=self._buffer_size
        )

        with local_file.open("wb") as out_file:
            if response in RSP.S_FL:
                if binary_mode:
                    out_file.write(content)
                else:
                    out_file.write(content.replace(b"\x00", b"\r\n"))
                logging.debug("received first block of file file %s", remote_path)

                while True:
                    response, content = self._llcom.telegram(
                        RSP.T_OK, payload=None, buffer_size=self._buffer_size
                    )
                    if response in RSP.S_FL:
                        if binary_mode:
                            out_file.write(content)
                        else:
                            out_file.write(content.replace(b"\x00", b"\r\n"))
                        logging.debug("received %d more bytes for file", len(content))
                    elif response in RSP.T_FD:
                        logging.info("finished loading file")
                        break
                    else:
                        if response in RSP.T_ER or response in RSP.T_BD:
                            logging.error(
                                "an error occurred while loading the first block of data for file %s : %s",
                                remote_path,
                                self._decode_error(content),
                            )
                        else:
                            logging.error(
                                "something went wrong while receiving file data %s",
                                remote_path,
                            )
                        return False
            else:
                if response in RSP.T_ER or response in RSP.T_BD:
                    logging.error(
                        "an error occurred while loading the first block of data for file %s : %s",
                        remote_path,
                        self._decode_error(content),
                    )
                    self._last_error_code = struct.unpack("!BB", content)
                else:
                    logging.error("could not load file with error %s", response)
                    self._last_error_code = None
                return False

        logging.info(
            "received %d bytes transfer complete for file %s to %s",
            local_file.stat().st_size,
            remote_path,
            local_file,
        )

        return True

    def read_plc_memory(self, address, mem_type, count=1):
        """Read data from plc memory.

        :param address: which memory location should be read, starts at 0 up to the max number for each type
        :param mem_type: what datatype to read
        :param count: how many elements should be read at a time, from 1 (default) up to 255 or max number
        :returns: a list with the data values
        :raises Exception: raises an Exception
        """
        if self._sys_par is None:
            self.get_system_parameter()

        self.login(login=Login.PLCDEBUG)

        if mem_type is MemoryType.MARKER:
            start_address = self._sys_par["Marker_Start"]
            max_count = self._sys_par["Markers"]
            mem_byte_count = 1
            unpack_string = "!?"
        elif mem_type is MemoryType.INPUT:
            start_address = self._sys_par["Input_Start"]
            max_count = self._sys_par["Inputs"]
            mem_byte_count = 1
            unpack_string = "!?"
        elif mem_type is MemoryType.OUTPUT:
            start_address = self._sys_par["Output_Start"]
            max_count = self._sys_par["Outputs"]
            mem_byte_count = 1
            unpack_string = "!?"
        elif mem_type is MemoryType.COUNTER:
            start_address = self._sys_par["Counter_Start"]
            max_count = self._sys_par["Counters"]
            mem_byte_count = 1
            unpack_string = "!?"
        elif mem_type is MemoryType.TIMER:
            start_address = self._sys_par["Timer_Start"]
            max_count = self._sys_par["Timers"]
            mem_byte_count = 1
            unpack_string = "!?"
        elif mem_type is MemoryType.BYTE:
            start_address = self._sys_par["Word_Start"]
            max_count = self._sys_par["Words"] * 2
            mem_byte_count = 1
            unpack_string = "!B"
        elif mem_type is MemoryType.WORD:
            start_address = self._sys_par["Word_Start"]
            max_count = self._sys_par["Words"]
            mem_byte_count = 2
            unpack_string = "<H"
        elif mem_type is MemoryType.DWORD:
            start_address = self._sys_par["Word_Start"]
            max_count = self._sys_par["Words"] / 4
            mem_byte_count = 4
            unpack_string = "<L"
        elif mem_type is MemoryType.STRING:
            start_address = self._sys_par["String_Start"]
            max_count = self._sys_par["Strings"]
            mem_byte_count = self._sys_par["String_Length"]
            unpack_string = "{}s".format(mem_byte_count)
        elif mem_type is MemoryType.INPUT_WORD:
            start_address = self._sys_par["Input_Word_Start"]
            max_count = self._sys_par["Input"]
            mem_byte_count = 2
            unpack_string = "<H"
        elif mem_type is MemoryType.OUTPUT_WORD:
            start_address = self._sys_par["Output_Word_Start"]
            max_count = self._sys_par["Output_Words"]
            mem_byte_count = 2
            unpack_string = "<H"
        else:
            raise Exception("unknown address type")

        if count > max_count:
            raise Exception("maximum number of values is %d" % max_count)

        if count > 0xFF:
            raise Exception("can't read more than 255 elements at a time")

        plc_values = list()

        if mem_type is MemoryType.STRING:
            # advance address if necessary
            address = address + (count - 1) * mem_byte_count
            for i in range(count):
                payload = bytearray()
                payload.extend(
                    struct.pack("!L", start_address + address + i * mem_byte_count)
                )
                payload.extend(struct.pack("!B", mem_byte_count))
                result = self._send_recive(CMD.R_MB, RSP.S_MB, payload=payload)
                if result:
                    logging.debug("read string %d", address + i * mem_byte_count)
                    plc_values.append(
                        struct.unpack(unpack_string, result)[0]
                        .rstrip(b"\x00")
                        .decode("utf8")
                    )
                else:
                    logging.error(
                        "failed to read string from address %d",
                        start_address + address + i * mem_byte_count,
                    )
                    return False
        else:
            payload = bytearray()
            payload.extend(struct.pack("!L", start_address + address))
            payload.extend(struct.pack("!B", count * mem_byte_count))
            result = self._send_recive(CMD.R_MB, RSP.S_MB, payload=payload)
            if result:
                logging.debug("read %d value(s) from address %d", count, address)
                for i in range(0, len(result), mem_byte_count):
                    plc_values.append(
                        struct.unpack(unpack_string, result[i : i + mem_byte_count])[0]
                    )
            else:
                logging.error(
                    "failed to read string from address %d", start_address + address
                )
                return False
        return plc_values

    def set_keyboard_access(self, unlocked):
        """Enable or disable the keyboard on the control. Requires access level MONITOR to work.

        :param bool unlocked: if True unlocks the keyboard. if false, input is set to locked
        :returns: True or False if command was executed successfully
        :rtype: bool
        """
        if not self.login(Login.MONITOR):
            logging.error("clould not log in as user MONITOR")
            return False

        payload = bytearray()
        if unlocked:
            payload.extend(struct.pack("!B", 0x00))
        else:
            payload.extend(struct.pack("!B", 0x01))

        result = self._send_recive(CMD.C_LK, RSP.T_OK, payload=payload)
        if result:
            if unlocked:
                logging.debug("command to unlock keyboard was successful")
            else:
                logging.debug("command to lock keyboard was successful")
            return True
        else:
            logging.warning("an error occurred changing the state of the keyboard lock")
        return False

    def get_machine_parameter(self, name):
        """Read machine parameter from control. Requires access INSPECT level to work.

        :param str name: name of the machine parameter. For iTNC the parameter number hase to be converted to string
        :returns: value of parameter or False if command not successful
        :rtype: str or bool
        """
        payload = bytearray()
        payload.extend(map(ord, name))
        payload.append(0x00)
        result = self._send_recive(CMD.R_MC, RSP.S_MC, payload=payload)
        if result:
            value = result.rstrip(b"\x00").decode("utf8")
            logging.debug("machine parameter %s has value %s", name, value)
            return value

        logging.warning("an error occurred while reading machine parameter %s", name)
        return False

    def set_machine_parameter(self, name, value, safe_to_disk=False):
        """Set machine parameter on control. Requires access PLCDEBUG level to work.
           Writing a parameter takes some time, make sure to set timeout sufficiently high!

        :param str name: name of the machine parameter. For iTNC the parameter number hase to be converted to string
        :param str value: new value of the machine parameter. There is no type checking, if the value can not be converted by the control an error will be sent.
        :param bool safe_to_disk: If True the new value will be written to the harddisk and stay permanent. If False (default) the value will only be available until the next reboot.

        :returns: True or False if command was executed successfully
        :rtype: bool
        """
        payload = bytearray()
        if safe_to_disk:
            payload.extend(struct.pack("!L", 0x00))
        else:
            payload.extend(struct.pack("!L", 0x01))
        payload.extend(map(ord, name))
        payload.append(0x00)
        payload.extend(map(ord, value))
        payload.append(0x00)

        result = self._send_recive(CMD.C_MC, RSP.T_OK, payload=payload)
        if result:
            logging.debug(
                "setting of machine parameter %s to value %s was successful",
                name,
                value,
            )
            return True

        logging.warning(
            "an error occurred while setting machine parameter %s to value %s",
            name,
            value,
        )
        return False

    def send_key_code(self, key_code):
        """Send key code to control. Behaves as if the associated key was pressed on the
           keyboard. Requires access MONITOR level to work. To work correctly you first
           have to lock the keyboard and unlock it afterwards!:

           set_keyboard_access(False)
           send_key_code(KeyCode.CE)
           set_keyboard_access(True)

        :param int key_code: code number of the keyboard key

        :returns: True or False if command was executed successfully
        :rtype: bool
        """
        if not self.login(Login.MONITOR):
            logging.error("clould not log in as user MONITOR")
            return False

        payload = bytearray()
        payload.extend(struct.pack("!H", key_code))

        result = self._send_recive(CMD.C_EK, RSP.T_OK, payload=payload)
        if result:
            logging.debug("sending the key code %d was successful", key_code)
            return True

        logging.warning("an error occurred while sending the key code %d", key_code)
        return False

    def get_spindle_tool_status(self):
        """Get information about the tool currently in the spindle

        :returns: tool information or False if something went wrong
        :rtype: dict
        """
        self.login(login=Login.DNC)
        payload = bytearray()
        payload.extend(struct.pack("!H", ParRRI.CURRENT_TOOL))
        result = self._send_recive(CMD.R_RI, RSP.S_RI, payload)
        if result:
            tool_info = decode_tool_information(result)
            logging.debug("successfully read info on current tool: %s", tool_info)
            return tool_info
        logging.warning(
            "an error occurred while querying current tool information. This does not work for all control types"
        )
        return False

    def get_override_info(self):
        """Get information about the override info

        :returns: override information or False if something went wrong
        :rtype: dict
        """
        self.login(login=Login.DNC)
        payload = bytearray()
        payload.extend(struct.pack("!H", ParRRI.OVERRIDE))
        result = self._send_recive(CMD.R_RI, RSP.S_RI, payload)
        if result:
            override_info = decode_override_information(result)
            logging.debug("successfully read override info: %s", override_info)
            return override_info
        logging.warning(
            "an error occurred while querying current override information. This does not work for all control types"
        )
        return False

    def get_error_messages(self):
        """Get information about the first or next error displayed on the control

        :param bool next_error: if True check if any further error messages are available
        :returns: error information or False if something went wrong
        :rtype: dict
        """
        messages = list()
        self.login(login=Login.DNC)

        payload = bytearray()
        payload.extend(struct.pack("!H", ParRRI.FIRST_ERROR))
        result = self._send_recive(CMD.R_RI, RSP.S_RI, payload)
        if result:
            messages.append(decode_error_message(result))
            payload = bytearray()
            payload.extend(struct.pack("!H", ParRRI.NEXT_ERROR))
            result = self._send_recive(CMD.R_RI, RSP.S_RI, payload)
            logging.debug("successfully read first error but further errors")

            while result:
                messages.append(decode_error_message(result))
                result = self._send_recive(CMD.R_RI, RSP.S_RI, payload)

            if self._last_error_code[1] == LSV2Err.T_ER_NO_NEXT_ERROR:
                logging.debug("successfully read all errors")
            else:
                logging.warning("an error occurred while querying error information.")

            return messages

        elif self._last_error_code[1] == LSV2Err.T_ER_NO_NEXT_ERROR:
            logging.debug("successfully read first error but no error active")
            return messages

        logging.warning(
            "an error occurred while querying error information. This does not work for all control types"
        )

        return False

    def _walk_dir(self, descend=True):
        """helber function to recursively search in directories for files

        :param bool descend: control if search should run recursively
        :returns: list of files found in directory
        :rtype: list
        """
        current_path = self.get_directory_info()["Path"]
        content = list()
        for entry in self.get_directory_content():
            if (
                entry["Name"] == "."
                or entry["Name"] == ".."
                or entry["Name"].endswith(":")
            ):
                continue
            current_fs_element = str(current_path + entry["Name"]).replace(
                "/", PATH_SEP
            )
            if entry["is_directory"] is True and descend is True:
                if self.change_directory(current_fs_element):
                    content.extend(self._walk_dir())
            else:
                content.append(current_fs_element)
        self.change_directory(current_path)
        return content

    def get_file_list(self, path=None, descend=True, pattern=None):
        """Get list of files in directory structure.

        :param str path: path of the directory where files should be searched. if None than the current directory is used
        :param bool descend: control if search should run recursiv
        :param str pattern: regex string to filter the file names
        :returns: list of files found in directory
        :rtype: list
        """
        if path is not None:
            if self.change_directory(path) is False:
                logging.warning("could not change to directory")
                return None

        if pattern is None:
            file_list = self._walk_dir(descend)
        else:
            file_list = list()
            for entry in self._walk_dir(descend):
                file_name = entry.split(PATH_SEP)[-1]
                if re.match(pattern, file_name):
                    file_list.append(entry)
        return file_list

    def read_data_path(self, path):
        """Read values from control via data path. Only works on iTNC controls.
        For ease of use, the path is formatted by replacing / by \\ and " by '.

        :param str path: data path from which to read the value.
        :returns: data value read from control formatted in nativ data type or None if reading was not successful
        """
        if not self.is_itnc():
            logging.warning(
                "Reading values from data path does not work on non iTNC controls!"
            )

        path = path.replace("/", PATH_SEP).replace('"', "'")

        self.login(login=Login.DATA)

        payload = bytearray()
        payload.extend(b"\x00")  # <- ???
        payload.extend(b"\x00")  # <- ???
        payload.extend(b"\x00")  # <- ???
        payload.extend(b"\x00")  # <- ???
        payload.extend(map(ord, path))
        payload.append(0x00)  # escape string

        result = self._send_recive(CMD.R_DP, RSP.S_DP, payload)

        if result:
            value_type = struct.unpack("!L", result[0:4])[0]
            if value_type == 2:
                data_value = struct.unpack("!h", result[4:6])[0]
            elif value_type == 3:
                data_value = struct.unpack("!l", result[4:8])[0]
            elif value_type == 5:
                data_value = struct.unpack("<d", result[4:12])[0]
            elif value_type == 8:
                data_value = result[4:].strip(b"\x00").decode("utf-8")
            elif value_type == 11:
                data_value = struct.unpack("!?", result[4:5])[0]
            elif value_type == 16:
                data_value = struct.unpack("!b", result[4:5])[0]
            elif value_type == 17:
                data_value = struct.unpack("!B", result[4:5])[0]
            else:
                raise Exception(
                    "unknown return type: %d for %s" % (value_type, result[4:])
                )

            logging.info(
                'successfuly read data path: %s and got value "%s"', path, data_value
            )
            return data_value
        logging.warning(
            'an error occurred while querying data path "%s". This does not work for all control types',
            path,
        )
        return None

    def get_axes_location(self):
        """Read axes location from control. Not fully documented, value of first byte unknown.
        - only tested on TNC640 programming station

        :returns: dictionary of axis label and current value
        """
        self.login(login=Login.DNC)

        payload = bytearray()
        payload.extend(struct.pack("!H", ParRRI.AXIS_LOCATION))

        result = self._send_recive(CMD.R_RI, RSP.S_RI, payload)
        if result:
            # unknown = result[0:1] # <- ???
            number_of_axes = struct.unpack("!b", result[1:2])[0]

            split_list = list()
            beginning = 2
            for i, byte in enumerate(result[beginning:]):
                if byte == 0x00:
                    value = result[beginning : i + 3]
                    split_list.append(value.strip(b"\x00").decode("utf-8"))
                    beginning = i + 3

            if len(split_list) != (2 * number_of_axes):
                raise Exception("error parsing axis values")

            axes_values = dict()
            for i in range(number_of_axes):
                axes_values[split_list[i + number_of_axes]] = float(split_list[i])

            logging.info("successfully read axes values: %s", axes_values)

            return axes_values

        logging.error("an error occurred while querying axes position")
        return False

    def grab_screen_dump(self, image_path: Path):
        """create screen_dump of current control screen and save it as bitmap"""
        if not self.login(Login.FILETRANSFER):
            logging.error("clould not log in as user FILE")
            return False

        temp_file_path = (
            DriveName.TNC
            + PATH_SEP
            + "screendump_"
            + datetime.now().strftime("%Y%m%d_%H%M%S")
            + ".bmp"
        )

        if not self.set_system_command(ParCCC.SCREENDUMP, temp_file_path):
            logging.error("screen dump was not created")
            return False

        if not self.recive_file(
            remote_path=temp_file_path, local_path=image_path, binary_mode=True
        ):
            logging.error("could not download screen dump from control")
            return False

        if not self.delete_file(temp_file_path):
            logging.error("clould not delete temporary file on control")
            return False

        logging.debug("successfully recived screen dump")
        return True
