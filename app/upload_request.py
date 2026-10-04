"""Close multipart temporary files even when parsing stops at a request limit."""

from flask import Request


class UploadRequest(Request):
    def _get_file_stream(self, total_content_length, content_type, filename=None, content_length=None):
        stream = super()._get_file_stream(total_content_length, content_type, filename, content_length)
        self.__dict__.setdefault("_upload_streams", []).append(stream)
        return stream

    def close(self):
        try:
            super().close()
        finally:
            # An interrupted multipart parse may never populate request.files,
            # so the base Request cannot see every stream it needs to close.
            for stream in self.__dict__.pop("_upload_streams", []):
                stream.close()
