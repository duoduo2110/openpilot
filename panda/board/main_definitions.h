#include "main_declarations.h"

// ********************* Globals **********************
uint8_t hw_type = 0;
board *current_board;
#ifdef STM32F4
uint16_t sound_output_level;
uint16_t spi_error_count;
#endif
uint32_t uptime_cnt = 0;

// heartbeat state
uint32_t heartbeat_counter = 0;
bool heartbeat_lost = false;
bool heartbeat_disabled = false;            // set over USB

// siren state
bool siren_enabled = false;
