#pragma once

#include <atomic>
#include <cstdint>
#include <mutex>
#include <string>
#include <vector>

#include <libusb-1.0/libusb.h>


#define TIMEOUT 0


// comms base class
class PandaCommsHandle {
public:
  PandaCommsHandle(std::string serial) {}
  virtual ~PandaCommsHandle() {}
  virtual void cleanup() = 0;

  std::string hw_serial;
  std::atomic<bool> connected = true;
  std::atomic<bool> comms_healthy = true;

  // HW communication
  virtual int control_write(uint8_t request, uint16_t param1, uint16_t param2, unsigned int timeout=TIMEOUT) = 0;
  virtual int control_read(uint8_t request, uint16_t param1, uint16_t param2, unsigned char *data, uint16_t length, unsigned int timeout=TIMEOUT) = 0;
  virtual int bulk_write(unsigned char endpoint, unsigned char* data, int length, unsigned int timeout=TIMEOUT) = 0;
  virtual int bulk_read(unsigned char endpoint, unsigned char* data, int length, unsigned int timeout=TIMEOUT) = 0;
};

class PandaUsbHandle : public PandaCommsHandle {
public:
  PandaUsbHandle(std::string serial);
  ~PandaUsbHandle();
  int control_write(uint8_t request, uint16_t param1, uint16_t param2, unsigned int timeout=TIMEOUT);
  int control_read(uint8_t request, uint16_t param1, uint16_t param2, unsigned char *data, uint16_t length, unsigned int timeout=TIMEOUT);
  int bulk_write(unsigned char endpoint, unsigned char* data, int length, unsigned int timeout=TIMEOUT);
  int bulk_read(unsigned char endpoint, unsigned char* data, int length, unsigned int timeout=TIMEOUT);
  void cleanup();

  static std::vector<std::string> list();

private:
  libusb_context *ctx = NULL;
  libusb_device_handle *dev_handle = NULL;
  std::recursive_mutex hw_lock;
  void handle_usb_issue(int err, const char func[]);
};
