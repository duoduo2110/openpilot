void clock_init(void) {
  // enable external oscillator
  register_set_bits(&(RCC->CR), RCC_CR_HSEON);
  volatile uint32_t hse_timeout = 1000000U;
  while ((RCC->CR & RCC_CR_HSERDY) == 0U) {
    if (--hse_timeout == 0U) break;
  }
  bool hse_ok = (RCC->CR & RCC_CR_HSERDY) != 0U;

  // divide things
  // AHB = 96MHz
  // APB1 = 48MHz
  // APB2 = 48MHz
  register_set(&(RCC->CFGR), RCC_CFGR_HPRE_DIV1 | RCC_CFGR_PPRE2_DIV2 | RCC_CFGR_PPRE1_DIV2, 0xFF7FFCF0U); // exclude SWS (hw-changed)

  // 16MHz crystal
  // PLLM: 8
  // PLLN: 96
  // PLLP: 2
  // PLLQ: 4
  // P output: 96MHz
  // Q output: 48MHz
  register_set(&(RCC->PLLCFGR), RCC_PLLCFGR_PLLQ_2 | RCC_PLLCFGR_PLLM_3 | RCC_PLLCFGR_PLLN_6 | RCC_PLLCFGR_PLLN_5 | (hse_ok ? RCC_PLLCFGR_PLLSRC_HSE : 0U), 0x7F437FFFU);

  // start PLL
  register_set_bits(&(RCC->CR), RCC_CR_PLLON);
  volatile uint32_t pll_timeout = 1000000U;
  while ((RCC->CR & RCC_CR_PLLRDY) == 0U) {
    if (--pll_timeout == 0U) break;
  }

  // Configure Flash prefetch, Instruction cache, Data cache and wait state
  // *** without this, it breaks ***
  register_set(&(FLASH->ACR), FLASH_ACR_ICEN | FLASH_ACR_DCEN | FLASH_ACR_LATENCY_5WS, 0x1F0FU);

  // switch to PLL
  register_set_bits(&(RCC->CFGR), RCC_CFGR_SW_PLL);
  while ((RCC->CFGR & RCC_CFGR_SWS) != RCC_CFGR_SWS_PLL);

  // *** running on PLL ***
}

