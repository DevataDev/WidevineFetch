import base64
import ctypes
import glob
import json
import sys
import re
from os.path import join, abspath, dirname, basename
from types import ModuleType
from typing import Any

import importlib.util
import pyperclip
import requests
import curl_cffi.requests as curl_requests
from PyQt5.QtCore import QThreadPool, pyqtSignal, pyqtSlot, QRunnable, QObject, QSettings
from PyQt5.QtGui import QIcon, QFont
from google.protobuf.json_format import MessageToDict

from PyQt5.QtWidgets import QWidget, QVBoxLayout, QTextEdit, QPushButton, QApplication, QMessageBox, QLineEdit, QLabel, \
    QGroupBox, QHBoxLayout, QCheckBox

from pywidevine import PSSH, Device, Cdm
from pywidevine.license_protocol_pb2 import SignedMessage, LicenseRequest, WidevinePsshData

POOL = QThreadPool.globalInstance()


class PlainTextEdit(QTextEdit):
    def insertFromMimeData(self, source):
        self.insertPlainText(source.text())


class WidevineFetch(QWidget):
    def __init__(self):
        """
        Parse 'Copy as fetch' of a license request and parse its data accordingly.
        No PSSH, Manifest, Cookies or License wrapping integration required.
        Author: github.com/DevLARLEY
        """

        super().__init__()
        self.resize(535, 535)
        self.setWindowTitle("WidevineFetch by github.com/DevLARLEY")
        self.setWindowIcon(QIcon(join(dirname(abspath(__file__)), "logo-small.png")))

        self.settings = QSettings("DevLARLEY", "WidevineFetch")
        if self.settings.value("impersonate") is None:
            self.settings.setValue("impersonate", False)

        layout = QVBoxLayout()

        self.text_edit = PlainTextEdit(self)
        self.text_edit.setReadOnly(True)
        mono = QFont()
        mono.setFamily("Courier New")
        self.text_edit.setFont(mono)
        layout.addWidget(self.text_edit)

        self.line_edit = QLineEdit(self)
        self.line_edit.setPlaceholderText(
            "Enter PSSH manually, if the request body is empty "
            "(e.g. when blocking a license and only the license certificate request is sent)."
        )
        layout.addWidget(self.line_edit)

        self.settings_box = QGroupBox("Settings", self)
        self.settings_layout = QHBoxLayout(self.settings_box)
        self.impersonate = QCheckBox("Impersonate Chrome", self.settings_box)
        self.impersonate.setChecked(bool(self.settings.value("impersonate", type=bool)))
        self.impersonate.clicked.connect(
            lambda _: self.settings.setValue("impersonate", self.impersonate.isChecked())
        )
        self.settings_layout.addWidget(self.impersonate)
        layout.addWidget(self.settings_box)

        self.process_button = QPushButton("Process", self)
        self.process_button.clicked.connect(self.start_process)
        layout.addWidget(self.process_button)

        self.label = QLabel("The fetch string is automatically retrieved from the clipboard", self)
        layout.addWidget(self.label)

        self.setLayout(layout)

    def info(self, message: str):
        self.text_edit.append(f'[INFO] {message}')

    def warning(self, message: str):
        self.text_edit.append(f'[WARNING] {message}')

    def error(self, message: str):
        QMessageBox.critical(
            self,
            "WidevineFetch/Error",
            message,
            buttons=QMessageBox.Ok,
            defaultButton=QMessageBox.Ok,
        )

    def start_process(self):
        self.text_edit.clear()

        try:
            clipboard = pyperclip.paste().replace('\n', '')
        except Exception as ex:
            self.error(f"Unable to get fetch from clipboard: {ex}")
            return

        print(f"User clipboard => \n{clipboard}")

        processor = AsyncProcessor(self.line_edit.text(), clipboard, self.impersonate.isChecked())
        processor.signals.info.connect(self.info)
        processor.signals.warning.connect(self.warning)
        processor.signals.error.connect(self.error)
        POOL.start(processor)

        self.line_edit.clear()


class ProcessorSignals(QObject):
    info = pyqtSignal(str)
    warning = pyqtSignal(str)
    error = pyqtSignal(str)


class AsyncProcessor(QRunnable):
    CDM_DIR = 'cdm'
    MODULE_DIR = 'modules'

    def __init__(
            self,
            pssh: str | None,
            read: str,
            impersonate: bool
    ):
        super().__init__()
        self.signals = ProcessorSignals()

        self.pssh = pssh
        self.read = read
        self.impersonate = impersonate

        self.module = None

    def log_info(self, message: str):
        self.signals.info.emit(message)

    def log_warning(self, message: str):
        self.signals.warning.emit(message)

    def log_error(self, message: str):
        self.signals.error.emit(message)

    @staticmethod
    def ensure_list(iterable):
        if isinstance(iterable, str):
            return [iterable]
        return iterable

    @staticmethod
    def has_arg(
            module: ModuleType,
            arg: str
    ) -> bool:
        if module:
            return arg in module.__dict__
        return False

    def import_module(
            self,
            file: str,
            path: str
    ):
        try:
            spec = importlib.util.spec_from_file_location(file, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as e:
            self.log_error(f"Unable to load module {file!r}: {e}")
            return

        if not isinstance(module, ModuleType):
            self.log_error(f"Module {file!r} is not a module")
            return

        return module

    def find_module(
            self,
            url: str
    ) -> ModuleType | None:
        if modules := glob.glob(join(dirname(abspath(__file__)), self.MODULE_DIR, '*.py')):
            for module in modules:
                imported = self.import_module(basename(module), module)
                if not imported:
                    continue
                if "REGEX" not in imported.__dict__:
                    self.log_error(f"Module {module!r} does not contain a 'REGEX' variable")
                    return
                for regex in self.ensure_list(imported.REGEX):
                    if re.fullmatch(regex, url):
                        self.log_info(f"Using module {basename(module)!r}")
                        return imported

    @pyqtSlot()
    def run(self):
        self.log_info("Parsing input...")

        if not (parsed := self._parse()):
            self.log_error("Unable to parse fetch string")
            return
        url, data = parsed

        if (method := data.get('method')) != 'POST':
            self.log_error(f"Expected a POST request, not {method!r}")
            return

        headers = data.get('headers')

        if not (body := data.get('body')):
            self.log_warning("Empty request body, continuing anyways")

        self.module = self.find_module(url)
        if self.has_arg(self.module, "IMPERSONATE"):
            self.impersonate = self.module.IMPERSONATE
            if self.impersonate:
                self.log_info("Forcing impersonation, as set in the currently loaded module")
        if self.has_arg(self.module, "MODIFY"):
            url, headers, body = self.module.MODIFY(url, headers, body)

        if keys := self._get_keys(
                url=url,
                headers=headers,
                body=body
        ):
            self.log_info('\n' + ' '.join(sum([['--key', i] for i in keys], [])))

    def _parse(self) -> tuple[str, dict] | None:
        search = re.search(
            r'.*fetch\(\"(.*)\",\s*{(.*)}\).*',
            self.read
        )
        if not search or len(search.groups()) < 2:
            return
        try:
            return search.group(1), json.loads('{' + search.group(2) + '}')
        except Exception:
            pass

    @staticmethod
    def _is_json(response: str) -> Any | None:
        try:
            return json.loads(response)
        except Exception:
            pass

    @staticmethod
    def _valid_base64_challenge(
            b64: str
    ) -> bool:
        return (
                b64 and b64[0] == 'C' and
                re.fullmatch(r"^([A-Za-z0-9+/]{4})*([A-Za-z0-9+/]{3}=|[A-Za-z0-9+/]{2}==)?$", b64)
        )

    def _replace_in_dict(
            self,
            d: dict,
            new: str
    ) -> dict:
        x = {}
        for k, v in d.items():
            if isinstance(v, dict):
                v = self._replace_in_dict(v, new)
            elif isinstance(v, list):
                v = self._replace_in_list(v, new)
            elif isinstance(v, str):
                if self._valid_base64_challenge(v):
                    v = new
            x[k] = v
        return x

    def _replace_in_list(
            self,
            l: list,
            new: str
    ) -> list:
        if (len(l) >= 50 or l == [8, 4]) and l[0] == 8 and all(isinstance(item, int) for item in l):
            return list(base64.b64decode(new))
        x = []
        for e in l:
            if isinstance(e, list):
                e = self._replace_in_list(e, new)
            elif isinstance(e, dict):
                e = self._replace_in_dict(e, new)
            elif isinstance(e, str):
                if self._valid_base64_challenge(e):
                    e = new
            x.append(e)
        return x

    def _find_in_dict(
            self,
            d: dict
    ) -> bytes:
        for k, v in d.items():
            if isinstance(v, dict):
                if r := self._find_in_dict(v):
                    return r
            elif isinstance(v, list):
                if r := self._find_in_list(v):
                    return r
            elif isinstance(v, str):
                if self._valid_base64_challenge(v):
                    return base64.b64decode(v)

    def _find_in_list(
            self,
            l: list
    ) -> bytes:
        if (len(l) >= 50 or l == [8, 4]) and l[0] == 8 and all(isinstance(item, int) for item in l):
            return bytes(l)

        for e in l:
            if isinstance(e, list):
                if r := self._find_in_list(e):
                    return r
            elif isinstance(e, dict):
                if r := self._find_in_dict(e):
                    return r
            elif isinstance(e, str):
                if self._valid_base64_challenge(e):
                    return base64.b64decode(e)

    @staticmethod
    def _substring_indices(
            content: bytes | str,
            sub: bytes | str
    ) -> list[int]:
        start, indices = 0, []
        while (start := content.find(sub, start)) != -1:
            indices.append(start)
            start += 1
        return indices

    @staticmethod
    def _get_pssh(
            content: bytes
    ) -> str | None:
        indices = AsyncProcessor._substring_indices(content, b'pssh')
        for i in indices:
            size = int.from_bytes(content[i - 8:i], "big") * 2
            pssh = PSSH(content[i - 8:i - 8 + size])
            if pssh.system_id == PSSH.SystemId.Widevine:
                return pssh.dumps()

    @staticmethod
    def _extract_pssh(
            message: str | bytes
    ) -> str | None:
        if not message:
            return

        print(f"License Request => {base64.b64encode(message).decode()}")

        if isinstance(message, str):
            message = base64.b64decode(message)

        signed_message = SignedMessage()
        try:
            signed_message.ParseFromString(message)
        except Exception:
            return ""

        if signed_message.type != SignedMessage.MessageType.Value("LICENSE_REQUEST"):
            return

        license_request = LicenseRequest()
        try:
            license_request.ParseFromString(signed_message.msg)
        except Exception:
            return ""

        request_json = MessageToDict(license_request)
        if not (content_id := request_json.get('contentId')):
            return

        if pssh_data := content_id.get('widevinePsshData'):
            return pssh_data.get('psshData')[0]

        if init_data := content_id.get('initData'):
            init_bytes = base64.b64decode(init_data.get('initData'))
            if pssh := AsyncProcessor._get_pssh(init_bytes):
                return pssh

        if webm_keyid := content_id.get('webmKeyId'):
            return base64.b64encode(
                WidevinePsshData(
                    key_ids=[base64.b64decode(webm_keyid.get('header'))],
                ).SerializeToString()
            ).decode()

    def _get_keys(
            self,
            url: str,
            headers: dict,
            body: Any
    ) -> list[str] | None:
        self.log_info("Retrieving challenge...")
        if self.has_arg(self.module, "GET_CHALLENGE"):
            challenge = self.module.GET_CHALLENGE(body)
            if isinstance(challenge, str):
                try:
                    challenge = base64.b64decode(challenge)
                except Exception as e:
                    self.log_error(f"Unable to decode base64 challenge from custom module: {e}")
                    return
        else:
            if j := self._is_json(body):
                if isinstance(j, dict):
                    challenge = self._find_in_dict(j)
                elif isinstance(j, list):
                    challenge = self._find_in_list(j)
                else:
                    self.log_error("Unsupported original json data")
                    return
            else:
                # assume bytes
                challenge = body
                if body:
                    try:
                        challenge = body.encode('ISO-8859-1')
                    except Exception as ex:
                        print(ex)
                        self.log_error("Unable to encode license request, please report this on GitHub.")
                        return

        if challenge == b'\x08\x04' and not self.pssh:
            self.log_error(
                "Certificate Request detected. "
                "Paste 'Copy as fetch' of the second license URL. The one that has the actual license request\n"
                "If you've blocked a request and see this message, enter the PSSH manually."
            )
            return

        self.log_info("Obtaining pssh...")
        if not (pssh := self._extract_pssh(challenge)):
            if pssh == "" and not (pssh := self.pssh):
                self.log_error(
                    "Failed to parse request body, enter PSSH manually.\n"
                    "This shouldn't happen though, please report this on GitHub."
                )
                return
            if pssh is None and not (pssh := self.pssh):
                self.log_error("Enter the PSSH manually, as the request body is empty")
                return

        if not (devices := glob.glob(join(dirname(abspath(__file__)), self.CDM_DIR, '*.wvd'))):
            self.log_error(f"No widevine devices (.wvd) detected inside the {self.CDM_DIR!r} directory")
            return

        device = Device.load(devices[0])

        cdm = Cdm.from_device(device)
        session_id = cdm.open()

        license_challenge = cdm.get_license_challenge(session_id, PSSH(pssh))

        self.log_info("Replacing challenge...")
        if self.has_arg(self.module, "SET_CHALLENGE"):
            set_challenge = self.module.SET_CHALLENGE(body, license_challenge)
            if isinstance(set_challenge, dict):
                data = dict(
                    json=set_challenge
                )
            elif isinstance(set_challenge, str):
                data = dict(
                    data=set_challenge
                )
            else:
                self.log_error(f"Unexpected SET_CHALLENGE return type {type(set_challenge)!r}")
                return
        else:
            if body is not None and (j := self._is_json(body)):
                if isinstance(j, dict):
                    data = dict(
                        json=self._replace_in_dict(j, base64.b64encode(license_challenge).decode('utf-8'))
                    )
                elif isinstance(j, list):
                    data = dict(
                        json=self._replace_in_list(j, base64.b64encode(license_challenge).decode('utf-8'))
                    )
                else:
                    self.log_error("Unsupported original json data")
                    return
            else:
                data = dict(
                    data=license_challenge
                )

        self.log_info("Sending request...")
        if self.impersonate:
            self.log_info("Impersonating Chrome...")
            response = curl_requests.post(
                url=url,
                headers=headers,
                impersonate="chrome",
                **data
            )
        else:
            response = requests.post(
                url=url,
                headers=headers,
                **data
            )

        if response.status_code != 200:
            self.log_error(f"Unable to obtain decryption keys, got error code {response.status_code}: {response.text}")
            return

        self.log_info("Retrieving license...")
        if self.has_arg(self.module, "GET_LICENSE"):
            licence = self.module.GET_LICENSE(response.text)
        else:
            if j := self._is_json(response.text):
                if isinstance(j, dict):
                    licence = self._find_in_dict(j)
                elif isinstance(j, list):
                    licence = self._find_in_list(j)
                else:
                    self.log_error("Unsupported returned json data")
                    return
            else:
                # assume bytes
                licence = response.content

        if not licence:
            self.log_error(f"Unable to locate license in response: {response.text}")
            return

        self.log_info("Parsing license...")
        try:
            cdm.parse_license(session_id, licence)
        except Exception as ex:
            self.log_error(f"Could not parse license {challenge!r}: {ex}")
            return

        return list(
            map(
                lambda key: f"{key.kid.hex}:{key.key.hex()}",
                filter(
                    lambda key: key.type == 'CONTENT',
                    cdm.get_keys(session_id)
                )
            )
        )


if __name__ == '__main__':
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("WidevineFetch")
    app = QApplication(sys.argv)
    wvf = WidevineFetch()
    wvf.show()
    sys.exit(app.exec_())
