"""Local admin entry point without opening or terminating shop browsers."""
import uvicorn
import license

if __name__ == '__main__':
    license.ADMIN_MODE = True
    uvicorn.run('web.app:app', host='127.0.0.1', port=3000)
