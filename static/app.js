"use strict";

document.addEventListener("submit", (event) => {
  const form = event.target;
  if (
    form.matches("form[data-confirm]") &&
    !window.confirm(form.dataset.confirm)
  ) {
    event.preventDefault();
  }
});

const chartData = document.getElementById("chart-data");
if (chartData && window.Chart) {
  const data = JSON.parse(chartData.textContent);
  const currency = new Intl.NumberFormat("ru-RU", {
    style: "currency",
    currency: "RUB",
  });
  Chart.defaults.font.family =
    "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
  Chart.defaults.font.size = 12;
  function palette() {
    const styles = getComputedStyle(document.documentElement);
    const color = (name) => styles.getPropertyValue(name).trim();
    return {
      label: color("--chart-label"),
      grid: color("--chart-grid"),
      full: color("--chart-full"),
      other: color("--chart-other"),
      missing: color("--chart-missing"),
      missingLabel: color("--chart-label-missing"),
      tooltip: color("--chart-tooltip"),
      surface: color("--surface"),
      lines: [1, 2, 3, 4, 5].map((index) => color("--chart-line-" + index)),
    };
  }
  Chart.defaults.color = palette().label;
  const commonOptions = {
    animation: false,
    responsive: true,
    maintainAspectRatio: false,
    interaction: { intersect: false, mode: "index" },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: palette().tooltip,
        padding: 13,
        cornerRadius: 10,
        callbacks: {
          label: (context) =>
            `${context.dataset.label || context.label}: ${currency.format(context.parsed.y)}`,
        },
      },
    },
    scales: {
      x: {
        grid: { display: false },
        border: { display: false },
        ticks: { color: palette().label },
      },
      y: {
        beginAtZero: true,
        border: { display: false },
        grid: { color: palette().grid, drawTicks: false },
        ticks: {
          padding: 12,
          maxTicksLimit: 5,
          callback: (value) => new Intl.NumberFormat("ru-RU").format(value),
        },
      },
    },
  };
  const bar = document.getElementById("comparison-chart");
  if (bar) {
    new Chart(bar, {
      type: "bar",
      data: {
        labels: data.labels,
        datasets: [
          {
            data: data.values,
            backgroundColor: data.complete.map((complete, index) =>
              !complete
                ? palette().missing
                : data.ids[index] === data.best
                  ? palette().full
                  : palette().other,
            ),
            borderRadius: 7,
            maxBarThickness: 92,
            categoryPercentage: 0.65,
          },
        ],
      },
      options: {
        ...commonOptions,
        plugins: {
          ...commonOptions.plugins,
          tooltip: {
            ...commonOptions.plugins.tooltip,
            callbacks: {
              label: (context) => currency.format(context.parsed.y),
              afterLabel: (context) =>
                data.complete[context.dataIndex]
                  ? "Полная корзина"
                  : "Сумма известных позиций",
            },
          },
        },
        layout: { padding: { top: 25 } },
      },
      plugins: [
        {
          id: "valueLabels",
          afterDatasetsDraw(chart) {
            const { ctx } = chart;
            ctx.save();
            ctx.font = "600 13px Segoe UI, sans-serif";
            ctx.textAlign = "center";
            chart.getDatasetMeta(0).data.forEach((element, index) => {
              ctx.fillStyle = data.complete[index]
                ? palette().label
                : palette().missingLabel;
              ctx.fillText(
                currency.format(data.values[index]),
                element.x,
                element.y - 12,
              );
            });
            ctx.restore();
          },
        },
      ],
    });
  }
  const line = document.getElementById("dynamics-chart");
  if (line) {
    const colors = palette().lines;
    new Chart(line, {
      type: "line",
      data: {
        labels: data.labels.map((value) =>
          new Date(value + "T12:00:00").toLocaleDateString("ru-RU"),
        ),
        datasets: data.datasets.map((dataset, index) => ({
          ...dataset,
          borderColor: colors[index % colors.length],
          backgroundColor: colors[index % colors.length],
          tension: 0,
          borderWidth: 3,
          borderDash: index % 2 ? [6, 4] : [],
          pointStyle: ["circle", "rectRot", "triangle", "rect", "star"][
            index % 5
          ],
          pointRadius: 5,
          pointHoverRadius: 7,
          pointBorderWidth: 3,
          pointBorderColor: palette().surface,
          spanGaps: false,
        })),
      },
      options: {
        ...commonOptions,
        plugins: {
          ...commonOptions.plugins,
          tooltip: {
            ...commonOptions.plugins.tooltip,
            callbacks: {
              label: (context) =>
                `${context.dataset.store_name || context.dataset.label}: ${currency.format(context.parsed.y)}`,
              afterLabel: (context) => {
                if (!context.dataset.complete) return "";
                if (context.dataset.complete[context.dataIndex])
                  return "Полная корзина";
                const missing =
                  context.dataset.missing[context.dataIndex] || [];
                return [
                  "Известные позиции",
                  ...missing.map((name) => "Нет цены: " + name),
                ];
              },
            },
          },
          legend: {
            display: true,
            position: "bottom",
            labels: { usePointStyle: true, padding: 24, boxHeight: 7 },
          },
        },
      },
    });
  }
  function applyChartTheme() {
    const colors = palette();
    Chart.defaults.color = colors.label;
    Object.values(Chart.instances).forEach((chart) => {
      chart.stop();
      chart.options.scales.x.ticks.color = colors.label;
      chart.options.scales.y.ticks.color = colors.label;
      chart.options.scales.y.grid.color = colors.grid;
      chart.options.plugins.tooltip.backgroundColor = colors.tooltip;
      if (chart.options.plugins.legend.labels) {
        chart.options.plugins.legend.labels.color = colors.label;
      }
      if (chart.config.type === "bar") {
        chart.data.datasets[0].backgroundColor = data.complete.map(
          (complete, index) => {
            if (!complete) return colors.missing;
            return data.ids[index] === data.best ? colors.full : colors.other;
          },
        );
      } else {
        chart.data.datasets.forEach((dataset, index) => {
          dataset.borderColor = colors.lines[index % colors.lines.length];
          dataset.backgroundColor = colors.lines[index % colors.lines.length];
          dataset.pointBorderColor = colors.surface;
        });
      }
      chart.update("none");
    });
  }
  applyChartTheme();
  window.addEventListener("themechange", applyChartTheme);
}
