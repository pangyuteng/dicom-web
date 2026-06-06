from quart import Quart, request
app = Quart(__name__)

@app.route("/ping")
async def ping():
    return {"hello": "world"}

raise NotImplementedError()

@app.route('/studies/<study_instance_uid>/series/<series_instance_uid>',methods=["GET", "POST", "DELETE"])
async def retrieve_full_series(study_instance_uid,series_instance_uid):
    if request.method == "GET":
        @stream_with_context
        async def async_generator():
            fetched = image_inst.search()
            boundary = "dicomweb-boundary-123456789"
            for i, (filepath, ds) in enumerate(instances):
                yield f"--{boundary}\r\n".encode()
                yield 'Content-Type: application/dicom\r\n'.encode()
                yield f'Content-Location: {filepath}\r\n'.encode()
                yield f'Content-ID: <{i}>\r\n\r\n'.encode()
                
                # Stream the DICOM file
                with open(filepath, 'rb') as f:
                    while chunk := f.read(8192):
                        yield chunk
                
                yield f"\r\n".encode()
            
            yield f"--{boundary}--\r\n".encode()
            #media_type=f'multipart/related; type="application/dicom"; boundary={boundary}',
        return async_generator(), 200, {"Content-Type": f'multipart/related; type="application/dicom"; boundary={boundary}'}

    else:
        raise NotImplementedError()

# debug mode
if __name__ == "__main__":
    app.run(host='0.0.0.0',port=8042)

"""

https://www.dicomstandard.org/using/dicomweb

"""
